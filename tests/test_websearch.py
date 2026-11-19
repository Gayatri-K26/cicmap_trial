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
    c.execute("INSERT INTO raw_pages(url, status_code, body, fetch_method) VALUES (?, 200, ?, 'search')",
              ('brave:site:drexel.edu "CS 380" syllabus', json.dumps(results)))
    found = websearch.candidates(c)
    assert [f["url"] for f in found] == [results[0]["url"]]
    assert websearch.summary(found) == {"Drexel University": {"courses": 1, "candidates": 1}}
