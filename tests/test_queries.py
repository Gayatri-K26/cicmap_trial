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
    sid = conn.execute("INSERT INTO syllabi(course_id, source_url, term, year) VALUES (1, 'u', 'Fall', 2026)").lastrowid
    conn.execute("INSERT INTO syllabus_outcomes(syllabus_id, position, text) VALUES (?, 1, 'Build agents')", (sid,))
    conn.execute("INSERT INTO syllabus_assessments(syllabus_id, name, weight_pct) VALUES (?, 'Final', 40)", (sid,))
    s = queries.syllabi(conn, 1)[0]
    assert s["outcomes"] == ["Build agents"]
    assert s["assessments"][0]["weight_pct"] == 40
    assert queries.totals(conn)["syllabi"] == 1
