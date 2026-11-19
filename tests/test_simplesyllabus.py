"""Simple Syllabus: picking the right library entries and parsing saved syllabus pages (no network)."""
from pathlib import Path

from cicmap import simplesyllabus as ss

FIX = Path(__file__).parent / "fixtures"


def item(title, term, sub="Data Mining And Machine Learning", vis="general_public", code="x"):
    return {"title": title, "term_name": term, "sub_title": sub, "visibility": vis, "code": code}


def test_pick_exact_code_campus_newest_first():
    items = [item("CS (WL) 37300 PIN", "WL Spring 2026", code="a"),
             item("CS (WL) 37300 LE2", "WL Spring 2026", code="b"),   # same term: one syllabus is enough
             item("CS (WL) 37300 PIN", "WL Fall 2026", code="c"),
             item("CS (FW) 37300 01", "PFW Fall 2026", code="d"),     # Fort Wayne campus, different course
             item("CS (WL) 24200 LE1", "WL Fall 2026", code="e"),     # fuzzy search hit, other course
             item("CS (WL) 37300 X", "WL Fall 2025", vis="private", code="f")]
    assert [i["code"] for i in ss.pick(items, "CS 37300", "(WL)")] == ["c", "a"]


def test_pick_without_campus_marker():
    items = [item("CSCI 4250 01", "Summer 2026", code="old"), item("CSCI 4250 01", "Summer 2027", code="new")]
    assert [i["code"] for i in ss.pick(items, "CSCI 4250", None)] == ["new", "old"]


def test_match_confidence():
    assert ss.match_confidence(item("", "", sub="Artificial Intelligence"), "Artificial Intelligence") == 0.95
    assert ss.match_confidence(item("", "", sub="Computational Intelligence"), "Artificial Intelligence") == 0.8


def test_parse_purdue_page():
    d = ss.parse_page((FIX / "simplesyllabus_purdue_cs37300.html").read_text())
    assert len(d["outcomes"]) == 6 and d["outcomes"][0].startswith("Identify key elements")
    assert [a["name"] for a in d["assessments"]] == ["Exams", "Homework Assignments", "Participation Activities"]
    assert d["assessments"][0]["kind"] == "exam"
    assert d["materials"] == []  # 'No textbooks required'
    assert d["summary"].startswith("This course will introduce")  # 'Credit Hours: 3.00.' stripped
    assert "Lowest passing grade" in d["grading_scale"]


def test_materials_edition_once_and_placeholders_skipped():
    def li(*lines):
        return "<li>" + "".join(f"<div>{x}</div>" for x in lines) + "</li>"
    html = ('<div class="component material-component"><h2>Texts</h2><ul>'
            + li("AI: A Modern Approach (4th Edition)", "Edition:", "4th Edition")
            + li("Speech and Language Processing", "Edition:", "3rd Edition")
            + li("Mining of Massive Datasets", "Edition:", "3") + li("See the detailed course information.")
            + "</ul></div>")
    assert [m["title"] for m in ss.parse_page(html)["materials"]] == [
        "AI: A Modern Approach (4th Edition)", "Speech and Language Processing (3rd Edition)",
        "Mining of Massive Datasets (3 ed.)"]


def test_grading_table_with_empty_cell():
    html = ('<div class="component"><h2>Grading</h2><table><tr><td>A</td><td></td><td>90%</td></tr>'
            '<tr><td>Final exam: 30%</td></tr></table></div>')
    assert ss.parse_page(html)["grading_scale"] == "A 90%; Final exam: 30%"


def test_parse_mga_page():
    d = ss.parse_page((FIX / "simplesyllabus_mga_csci4250.html").read_text())
    assert len(d["outcomes"]) == 7
    assert d["materials"] == [{"kind": "textbook", "title": "ARTIFICIAL INTELLIGENCE (4TH 21 ed.)",
                               "authors": "RUSSELL"}]  # not the 'Wellness Resources' advice list
    assert d["instructor"] == "Jongho Seol" and d["delivery_mode"] == "Fully at a distance"
    assert d["grading_scale"].startswith("Letter Grade Percentage; A 90% - 100%")
    assert "Course-Level AI Policy" in d["raw_text"]  # unrecognized sections kept for a later pass
