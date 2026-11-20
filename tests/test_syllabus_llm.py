"""LLM pass over syllabi, with a fake LLM: guard rails and fill-only-empty behavior."""
import json
from pathlib import Path

from cicmap import db, syllabi, syllabus_llm

TEXT = """CS 422 Data Mining, Spring 2013
Instructor Gady Agam
Grading
assignment 1
5%
midterm exam
15%
final exam
80%
Textbook: Introduction to Data Mining, Tan, Steinbach and Kumar
Week 1 Exploring data"""


class FakeLLM:
    enabled = True

    def __init__(self, answer):
        self.answer = answer

    def json(self, purpose, version, system, user, max_tokens=4000):
        return self.answer


ANSWER = {"outcomes": ["Build quantum blockchains"],                       # not in the text: dropped
          "topics": [{"week": "1", "title": "Exploring data"}],
          "assessments": [{"name": "assignment 1", "weight_pct": 5}, {"name": "midterm exam", "weight_pct": 15},
                          {"name": "final exam", "weight_pct": 80}],
          "materials": [{"title": "Introduction to Data Mining", "authors": "Tan, Steinbach and Kumar",
                         "kind": "textbook", "required": True},
                        {"title": "Deep Learning", "authors": "Goodfellow"}],  # not in the text: dropped
          "instructor": "Gady Agam", "term": "Spring", "year": 2013, "grading_scale": None}


def test_checked_keeps_only_what_the_text_supports():
    got = syllabus_llm.checked(ANSWER, TEXT)
    assert got["outcomes"] == []
    assert [m["title"] for m in got["materials"]] == ["Introduction to Data Mining"]
    assert [(a["name"], a["kind"], a["weight_pct"]) for a in got["assessments"]] == [
        ("assignment 1", "homework", 5.0), ("midterm exam", "exam", 15.0), ("final exam", "exam", 80.0)]
    assert (got["instructor"], got["term"], got["year"]) == ("Gady Agam", "Spring", 2013)


def test_weights_not_adding_up_are_dropped():
    bad = dict(ANSWER, assessments=[{"name": "midterm exam", "weight_pct": 15}, {"name": "final exam",
                                                                                  "weight_pct": 30}])
    assert [a["weight_pct"] for a in syllabus_llm.checked(bad, TEXT)["assessments"]] == [None, None]


def _db_with_doc(doc):
    c = db.connect(Path(":memory:"))
    c.execute("INSERT INTO institutions(id, name) VALUES (1, 'Illinois Tech')")
    c.execute("INSERT INTO courses(id, institution_id, code, title, is_ai) VALUES (1, 1, 'CS 422', 'Data Mining', 1)")
    sid = syllabi.save_document(c, [1], "http://x.edu/~agam/cs422", "faculty", dict(doc, raw_text=TEXT), 0.9, "x")
    return c, sid


def test_fill_only_empty_sections_and_mark_llm():
    c, sid = _db_with_doc({"instructor": "Prof. Agam", "outcomes": [], "topics": [], "assessments": [],
                           "materials": []})
    filled = syllabus_llm.fill(c, FakeLLM(ANSWER), sid)
    assert set(filled) == {"topics", "assessments", "materials", "term", "year"}
    s = c.execute("SELECT instructor, parse_method, extra_json FROM syllabi WHERE id = ?", (sid,)).fetchone()
    assert s["instructor"] == "Prof. Agam"  # the deterministic value is kept
    assert s["parse_method"] == "deterministic+llm"
    assert "materials" in json.loads(s["extra_json"])["llm_sections"]
    assert syllabus_llm.fill(c, FakeLLM(ANSWER), sid) == []  # nothing left to fill


def test_fill_adds_weights_to_assessments_found_without_them():
    names = ("Assignment 1", "Midterm exam", "Final exam")
    c, sid = _db_with_doc({"assessments": [{"name": n, "kind": None, "weight_pct": None} for n in names]})
    assert "assessment weights" in syllabus_llm.fill(c, FakeLLM(ANSWER), sid)
    assert [r[0] for r in c.execute("SELECT weight_pct FROM syllabus_assessments WHERE syllabus_id = ? "
                                    "ORDER BY id", (sid,))] == [5, 15, 80]


def test_partial_weight_merge_is_refused():
    c, sid = _db_with_doc({"assessments": [{"name": "Final exam", "kind": "exam", "weight_pct": None},
                                           {"name": "Quizzes", "kind": "quiz", "weight_pct": None}]})
    filled = syllabus_llm.fill(c, FakeLLM(ANSWER), sid)  # only 'final exam' (80) matches: 80% isn't a total
    assert "assessment weights" not in filled
    assert c.execute("SELECT COUNT(*) FROM syllabus_assessments WHERE syllabus_id = ? AND weight_pct IS NOT NULL",
                     (sid,)).fetchone()[0] == 0
