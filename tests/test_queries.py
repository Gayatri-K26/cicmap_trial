"""Read-side queries used by the CLI and the UI."""
from pathlib import Path

import pytest

from cicmap import db, queries


@pytest.fixture
def conn():
    c = db.connect(Path(":memory:"))
    sid, _ = queries.add_source(c, "Test U", "BS AI", "https://catalog.test.edu/ai")
    c.execute("INSERT INTO institutions(id, name) VALUES (1, 'Test U')")
    c.execute("INSERT INTO programs(id, source_id, institution_id, name, degree, source_url) "
              "VALUES (1, ?, 1, 'BS AI', 'BS', 'https://catalog.test.edu/ai')", (sid,))
    c.executemany("INSERT INTO courses(id, institution_id, code, title, is_ai, is_stub) VALUES (?, 1, ?, ?, ?, ?)",
                  [(1, "CS 480", "Intro to AI", 1, 0), (2, "CS 331", None, 0, 1), (3, "CS 401", None, 0, 1),
                   (4, "CS 580", "Advanced AI", 1, 0)])
    c.executemany("INSERT INTO program_courses(program_id, course_id, requirement_group) VALUES (1, ?, 'Core')",
                  [(1,), (4,)])
    c.executemany("INSERT INTO course_prereqs(course_id, kind, group_no, prereq_course_id, condition_text, "
                  "min_grade) VALUES (?, 'prereq', ?, ?, ?, ?)",
                  [(1, 1, 2, None, "C"), (1, 1, 3, None, None), (1, 2, None, "junior standing", None),
                   (4, 1, 1, None, None)])
    c.execute("UPDATE program_sources SET status = 'extracted'")
    c.commit()
    return c


def test_add_source_reports_existing(conn):
    sid, existed = queries.add_source(conn, "Test U", "BS AI", " https://catalog.test.edu/ai ")
    assert existed and sid == 1
    assert queries.add_source(conn, "Other U", "MS AI", "https://other.edu/ms")[1] is False


def test_sources_count_ai_courses(conn):
    s = queries.source(conn, 1)
    assert s["program_id"] == 1 and s["ai_courses"] == 2


def test_ai_courses_exclude_stubs(conn):
    assert [c["code"] for c in queries.ai_courses(conn, 1)] == ["CS 480", "CS 580"]


def test_course_prereqs_and_required_by(conn):
    c = queries.course(conn, 1)
    assert c["prereqs"] == [["CS 331 (min C)", "CS 401"], ["junior standing"]]
    assert queries.format_groups(c["prereqs"]) == "(CS 331 (min C) OR CS 401)  AND  junior standing"
    assert queries.course(conn, 1)["required_by"] == ["CS 580"]
    assert queries.find_course(conn, "test", "cs 580")["title"] == "Advanced AI"


def test_syllabi_with_children(conn):
    assert queries.syllabi(conn, 1) == []
    sid = conn.execute("INSERT INTO syllabi(source_url, term, year) VALUES ('u', 'Fall', 2026)").lastrowid
    conn.execute("INSERT INTO syllabus_courses(syllabus_id, course_id, match_confidence) VALUES (?, 1, 0.9)", (sid,))
    conn.execute("INSERT INTO syllabus_outcomes(syllabus_id, position, text) VALUES (?, 1, 'Build agents')", (sid,))
    conn.execute("INSERT INTO syllabus_assessments(syllabus_id, name, weight_pct) VALUES (?, 'Final', 40)", (sid,))
    s = queries.syllabi(conn, 1)[0]
    assert s["outcomes"] == ["Build agents"]
    assert s["assessments"][0]["weight_pct"] == 40 and s["match_confidence"] == 0.9
    assert queries.totals(conn)["syllabi"] == 1 and queries.totals(conn)["courses_with_syllabus"] == 1
    conn.execute("INSERT INTO syllabi(source_url, source_kind) VALUES ('cat', 'catalog')")
    assert queries.totals(conn)["syllabi"] == 1  # catalog entries are not syllabi


def test_cross_listed_syllabus_belongs_to_both_courses(conn):
    sid = conn.execute("INSERT INTO syllabi(source_url) VALUES ('cs480-580.pdf')").lastrowid
    conn.executemany("INSERT INTO syllabus_courses(syllabus_id, course_id) VALUES (?, ?)", [(sid, 1), (sid, 4)])
    assert [s["id"] for s in queries.syllabi(conn, 1)] == [sid] == [s["id"] for s in queries.syllabi(conn, 4)]
    assert {c["code"]: c["syllabi"] for c in queries.ai_courses(conn, 1)} == {"CS 480": 1, "CS 580": 1}


def test_migration_drops_empty_old_syllabi_table(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE syllabi (id INTEGER PRIMARY KEY, course_id INTEGER, source_url TEXT)")
    old.commit()
    old.close()
    c = db.connect(path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(syllabi)")}
    assert "course_id" not in cols and "source_kind" in cols


def test_prereq_cycles(conn):
    assert queries.prereq_cycles(conn) == []  # CS 480 -> CS 331/CS 401, CS 580 -> CS 480: no loop
    conn.execute("INSERT INTO course_prereqs(course_id, kind, group_no, prereq_course_id) VALUES (2, 'prereq', 1, 4)")
    assert queries.prereq_cycles(conn) == [["CS 480", "CS 331", "CS 580", "CS 480"]]
    assert queries.prereq_cycles(conn, institution_id=99) == []


def test_mutual_corequisites_are_not_a_loop(conn):
    conn.executemany("INSERT INTO course_prereqs(course_id, kind, group_no, prereq_course_id) "
                     "VALUES (?, 'coreq', 1, ?)", [(1, 4), (4, 1)])  # lecture and lab list each other
    assert queries.prereq_cycles(conn) == []


def test_course_shows_syllabus_prereqs_only_when_catalog_has_none(conn):
    from cicmap import syllabi
    syllabi.save_document(conn, [4], "https://p.edu/doc", "portal", {"term": "Fall", "year": 2026,
                          "extra": {"prerequisites": "CS 480, Minimum Grade of C"}}, 0.95, "portal_code")
    sp = queries.course(conn, 4)["syllabus_prereq"]
    assert (sp["text"], sp["source_kind"], sp["year"]) == ("CS 480, Minimum Grade of C", "portal", 2026)
    conn.execute("UPDATE courses SET prereq_text = 'CS 480' WHERE id = 4")
    assert queries.course(conn, 4)["syllabus_prereq"] is None  # the catalog's own text wins


def test_course_dropped_from_every_program_stops_counting_as_ai(conn):
    from cicmap.pipeline import unmark_dropped
    conn.execute("DELETE FROM program_courses WHERE course_id = 4")  # re-extraction no longer lists CS 580
    assert unmark_dropped(conn, 1) == 1
    assert conn.execute("SELECT is_ai FROM courses WHERE id = 4").fetchone()[0] == 0
    assert conn.execute("SELECT is_ai FROM courses WHERE id = 1").fetchone()[0] == 1  # still listed
