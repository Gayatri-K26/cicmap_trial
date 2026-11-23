"""Brave search helpers (no network: results come from the raw_pages cache)."""
import json
from pathlib import Path

from cicmap import db, websearch


def test_school_domain():
    assert websearch.school_domain("catalog.purdue.edu") == "purdue.edu"
    assert websearch.school_domain("kean.smartcatalogiq.com") == "kean.edu"
    assert websearch.school_domain("hallmark.edu") == "hallmark.edu"


def test_usable_drops_catalogs_portals_and_lms():
    assert websearch.usable("https://www.cs.drexel.edu/~dds26/courses/syllabi/CS380_Syllabus.pdf", "drexel.edu")
    assert not websearch.usable("https://catalog.drexel.edu/x", "drexel.edu")
    assert not websearch.usable("https://miamioh.simplesyllabus.com/doc/x", "miamioh.edu")
    assert not websearch.usable("https://canvas.purdue.edu/courses/1", "purdue.edu")
    assert not websearch.usable("https://www.gmu.edu/cs480.pdf", "drexel.edu")  # another school


def test_candidates_use_cached_results():
    c = db.connect(Path(":memory:"))
    c.execute("INSERT INTO institutions(id, name, domain) VALUES (1, 'Drexel University', 'catalog.drexel.edu')")
    c.execute("INSERT INTO courses(id, institution_id, code, title, is_ai) VALUES (1, 1, 'CS 380', 'AI', 1)")
    results = [{"url": "https://www.cs.drexel.edu/~dds26/CS380_Syllabus.pdf", "title": "CS 380 Syllabus"},
               {"url": "https://catalog.drexel.edu/cs380", "title": "Catalog"}]
    for i, q in enumerate(websearch.queries_for("drexel.edu", "CS 380")):  # all cached: no network in tests
        c.execute("INSERT INTO raw_pages(url, status_code, body, fetch_method) VALUES (?, 200, ?, 'search')",
                  (f"brave:{q}", json.dumps(results if i < 2 else [])))  # same page from two queries
    found = websearch.candidates(c)
    assert [f["url"] for f in found] == [results[0]["url"]]
    assert websearch.summary(found) == {"Drexel University": {"courses": 1, "candidates": 1}}


SYLLABUS = """CS 380: Artificial Intelligence - Fall 2024
Instructor: Dr. Smith. Office hours: Tue 2-4pm.
Prerequisite: CS 260 and CS 270.
Textbook: Artificial Intelligence: A Modern Approach.
Grading: Homework 40%, Midterm 25%, Final exam 35%.
"""


def test_assess_accepts_a_syllabus():
    assert websearch.assess(SYLLABUS, "CS 380", "Artificial Intelligence", "https://x.edu/s.pdf") == \
        (0.9, "code_and_title_in_text")
    assert websearch.assess(SYLLABUS, "CS 380", "Intro to AI", "https://x.edu/s.pdf")[0] == 0.75


def test_assess_rejects_lists_mentions_and_non_syllabi():
    degree_map = "CS 380 AI\n" + "\n".join(f"CS {100 + i} Course" for i in range(30)) + "\ngrading exam homework"
    assert websearch.assess(degree_map, "CS 380", None, "https://x.edu/map") is None       # lists many courses
    late_mention = "Department news. " * 200 + SYLLABUS
    assert websearch.assess(late_mention, "CS 380", None, "https://x.edu/news") is None    # course not up top
    assert websearch.assess("CS 380 Artificial Intelligence. Fun!", "CS 380", None, "u") is None  # no signals


def test_code_in_url_forms():
    assert websearch.code_in("CS 380", "https://x.edu/CS380_Syllabus.pdf")
    assert websearch.code_in("CS 380", "cs-380 notes")
    assert not websearch.code_in("CS 380", "CS 3801")


def test_skip_url():
    assert websearch.skip_url("https://x.edu/~so367/2017/CS380/assignments.html")
    assert websearch.skip_url("https://x.edu/~so367/2017/CS380/CS380-W1-Introduction.pdf")
    assert not websearch.skip_url("https://x.edu/~ab/capstone-project-syllabus.pdf")  # says syllabus
    assert not websearch.skip_url("https://x.edu/~so367/2017/CS380/intro.html")


def test_discover_removes_documents_that_no_longer_pass():
    from cicmap import syllabi
    c = db.connect(Path(":memory:"))
    c.execute("INSERT INTO institutions(id, name, domain) VALUES (1, 'Drexel University', 'catalog.drexel.edu')")
    c.execute("INSERT INTO courses(id, institution_id, code, title, is_ai) VALUES (1, 1, 'CS 380', 'AI', 1)")
    old = "https://www.cs.drexel.edu/~so367/CS380/assignment3.html"
    syllabi.save_document(c, [1], old, "faculty", {}, 0.75, "code_in_text")
    syllabi.save_document(c, [1], "https://catalog.drexel.edu/cs380", "catalog", {}, 1.0, "catalog")
    for q in websearch.queries_for("drexel.edu", "CS 380"):
        c.execute("INSERT INTO raw_pages(url, status_code, body, fetch_method) VALUES (?, 200, ?, 'search')",
                  (f"brave:{q}", json.dumps([{"url": old, "title": "Assignment 3"}])))
    counts = websearch.discover(c)
    assert counts["removed"] == 1 and counts["kept"] == 0
    assert [r[0] for r in c.execute("SELECT source_kind FROM syllabi")] == ["catalog"]  # catalog untouched
