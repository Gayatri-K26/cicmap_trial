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
    assert doc["extra"] == {"offered": "Fall and Winter"}


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
