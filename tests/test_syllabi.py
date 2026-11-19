"""Syllabus documents: catalog entries as the first source."""
import json
from pathlib import Path

import pytest

from cicmap import db, queries, syllabi


def test_split_numbered_outcomes():
    text = ("1. Use data processing and implement algorithms using Python 3. 2. Determine how to represent "
            "knowledge. 3. Build a machine learning system.")
    assert syllabi.split_outcomes(text) == [
        "Use data processing and implement algorithms using Python 3.",
        "Determine how to represent knowledge.", "Build a machine learning system."]


def test_split_outcomes_bullets_and_single():
    assert syllabi.split_outcomes("• Analyze data\n• Build models") == ["Analyze data", "Build models"]
    assert syllabi.split_outcomes("Students design a data science solution") == [
        "Students design a data science solution"]
    assert syllabi.split_outcomes("") == [] and syllabi.split_outcomes(None) == []


def test_catalog_document_maps_labels():
    doc = syllabi.catalog_document({
        "description": "Intro to ML.",
        "extra_json": json.dumps({"Learning Outcomes": "1. Fit models. 2. Evaluate them.", "Lecture": "3",
                                  "Lab": "0", "When Taught": "Fall and Winter", "Repeat Status": "No"})})
    assert doc["summary"] == "Intro to ML."
    assert doc["outcomes"] == ["Fit models.", "Evaluate them."]
    assert doc["contact_hours"] == "Lecture: 3, Lab: 0"
    assert "extra" not in doc  # terms offered stay with the course details


@pytest.fixture
def conn():
    c = db.connect(Path(":memory:"))
    c.execute("INSERT INTO institutions(id, name) VALUES (1, 'Test U')")
    c.executemany(
        "INSERT INTO courses(id, institution_id, code, title, description, extra_json, source_url, is_ai) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        [(1, "CS 373", "Data Mining", "Patterns in data.",
          json.dumps({"Learning Outcomes": "1. Mine data. 2. Choose algorithms."}), "https://cat.test.edu/cs373", 1),
         (2, "CS 101", "Intro", "Not AI.", None, "https://cat.test.edu/cs101", 0)])
    return c


def test_load_catalog_is_idempotent(conn):
    assert syllabi.load_catalog(conn) == 1  # only AI courses
    assert syllabi.load_catalog(conn) == 1
    docs = queries.syllabi(conn, 1)
    assert len(docs) == 1
    d = docs[0]
    assert (d["source_kind"], d["match_method"], d["match_confidence"]) == ("catalog", "catalog", 1.0)
    assert d["outcomes"] == ["Mine data.", "Choose algorithms."] and d["summary"] == "Patterns in data."
    assert queries.syllabi(conn, 2) == []


def test_each_fact_shown_once(conn):
    conn.execute("UPDATE courses SET extra_json = ? WHERE id = 1", (json.dumps(
        {"Learning Outcomes": "1. Mine data. 2. Choose.", "Lecture": "3", "When Taught": "Fall"}),))
    syllabi.load_catalog(conn)
    assert queries.course(conn, 1)["extra"] == {"When Taught": "Fall"}  # outcomes and hours go to the syllabus
    p = queries.syllabus_profile(conn, 1)
    assert "summary" in p["missing"]  # the catalog description is not repeated as a syllabus summary
    assert p["sections"]["contact_hours"]["value"] == "Lecture: 3"
    syllabi.save_document(conn, [1], "s.pdf", "faculty", {"summary": "Fuller overview."}, 0.9, "x")
    assert queries.syllabus_profile(conn, 1)["sections"]["summary"]["value"] == "Fuller overview."


def test_profile_combines_sources(conn):
    syllabi.load_catalog(conn)
    syllabi.save_document(conn, [1], "https://prof.test.edu/cs373.pdf", "faculty", {
        "term": "Fall", "year": 2024, "assessments": [{"name": "Final", "weight_pct": 30}],
        "materials": [{"title": "Introduction to Data Mining", "authors": "Tan"}]}, 0.9, "code_in_text")
    p = queries.syllabus_profile(conn, 1)
    s = p["sections"]
    assert s["outcomes"]["source"]["source_kind"] == "catalog"         # only the catalog has outcomes
    assert s["assessments"]["source"]["source_kind"] == "faculty"      # only the PDF has grading
    assert s["materials"]["value"][0]["title"] == "Introduction to Data Mining"
    assert "instructor" in p["missing"] and "outcomes" not in p["missing"]
    assert [d["source_kind"] for d in p["documents"]] == ["faculty", "catalog"]  # real syllabi first


def _two_schools(conn):
    conn.execute("INSERT INTO institutions(id, name) VALUES (2, 'Other U')")
    conn.execute("INSERT INTO courses(id, institution_id, code, title, description, is_ai) "
                 "VALUES (3, 2, 'AI 300', 'Agents', 'Covers reinforcement learning.', 1)")
    syllabi.load_catalog(conn)
    syllabi.save_document(conn, [1], "a.pdf", "faculty", {"materials": [
        {"title": "Artificial Intelligence: A Modern Approach (4th ed.)", "authors": "Russell, Norvig"}]}, 0.9, "x")
    syllabi.save_document(conn, [3], "b.pdf", "faculty", {
        "materials": [{"title": "Artificial Intelligence - A Modern Approach, 3rd edition"},
                      {"title": "Deep Learning"}],
        "topics": [{"week": "5", "title": "Mining association rules"}]}, 0.9, "x")


def test_search_finds_topic_across_schools(conn):
    _two_schools(conn)
    found = {s["institution"]: s["courses"] for s in queries.search_syllabi(conn, "mining")}
    assert [c["code"] for c in found["Test U"]] == ["CS 373"]
    assert found["Test U"][0]["found_in"] == ["title"]
    assert found["Other U"][0]["found_in"] == ["topics"]
    data = {s["institution"]: s["courses"] for s in queries.search_syllabi(conn, "data")}
    assert set(data["Test U"][0]["found_in"]) == {"title", "summary", "outcomes"}
    assert [s["institution"] for s in queries.search_syllabi(conn, "reinforcement")] == ["Other U"]
    assert queries.search_syllabi(conn, "Intro") == []  # CS 101 is not an AI course


def test_common_materials_match_editions(conn):
    _two_schools(conn)
    common = queries.common_materials(conn)
    assert len(common) == 1  # 'Deep Learning' is used at one school only
    assert common[0]["school_count"] == 2
    assert common[0]["schools"] == {"Other U": ["AI 300"], "Test U": ["CS 373"]}


WEB_SYLLABUS = """CS 380: Artificial Intelligence\tSpring 2021
Instructor:
Office Hours
Instructor: Dr. Ada Lovelace
The course objectives are to:
• Describe search methods for hard
problems in AI.
• Practice programming AI algorithms.
Grading: Homework 40%, Midterm 25%, Final exam 35%
Grading scale: A 90%, B 80%
Week 1: Intro and agents
Week 2 - Search
"""


def test_parse_text_takes_what_it_can_read():
    d = syllabi.parse_text(WEB_SYLLABUS)
    assert (d["term"], d["year"], d["instructor"]) == ("Spring", 2021, "Dr. Ada Lovelace")  # not 'Office Hours'
    assert d["outcomes"] == ["Describe search methods for hard problems in AI.", "Practice programming AI algorithms."]
    assert [(a["name"], a["kind"], a["weight_pct"]) for a in d["assessments"]] == [
        ("Homework", "homework", 40.0), ("Midterm", "exam", 25.0), ("Final exam", "exam", 35.0)]
    assert d["topics"] == [{"week": "1", "title": "Intro and agents"}, {"week": "2", "title": "Search"}]
    assert "Grading scale" in d["raw_text"]


def test_weights_that_dont_add_up_are_dropped():
    assert syllabi.parse_text("Homework 40%\nAttendance matters: 5% of students fail")["assessments"] == []
