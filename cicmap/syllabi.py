"""Syllabus documents and their sections.

A course's syllabus is assembled from several source documents, each stored as one `syllabi` row linked
to its course(s) through `syllabus_courses`. The first source is the catalog entry we already scraped:
it often has the description, learning outcomes, contact hours and terms offered. Real syllabi found on
the web add what catalogs lack (grading, textbooks, weekly topics).
"""
import json
import re
from typing import Iterable, List, Optional

# Catalog labels (as stored in courses.extra_json) that describe how a course is taught go to the syllabus;
# everything else stays with the course details. Add a school's label variant here.
HOURS_LABELS = ("Lecture", "Lecture Hours", "Class Hours", "Lab", "Lab Hours", "Laboratory Hours")


def is_syllabus_label(label: str) -> bool:
    return "outcome" in label.lower() or label in HOURS_LABELS

_NUMBERED = re.compile(r"(?:^|(?<=\s))(\d{1,2})[.)]\s+")


def split_outcomes(text: Optional[str]) -> List[str]:
    """'1. Do X. 2. Do Y.' -> ['Do X.', 'Do Y.']. Only splits on a run of numbers counting up from 1,
    so a stray number inside a sentence ('Python 3. ') doesn't cut it. Bullets and line breaks also split."""
    if not text or not text.strip():
        return []
    cuts, want = [], 1
    for m in _NUMBERED.finditer(text):
        if int(m.group(1)) == want:
            cuts.append(m)
            want += 1
    if len(cuts) >= 2:
        parts = [text[a.end():(b.start() if b else len(text))] for a, b in zip(cuts, cuts[1:] + [None])]
    else:
        parts = re.split(r"\s*[•\n]\s*", text)
    return [p.strip(" ;") for p in parts if p.strip(" ;")]


def catalog_document(course: dict) -> dict:
    """The syllabus sections a catalog entry provides. `course` is a courses row as a dict."""
    extra = json.loads(course["extra_json"]) if course.get("extra_json") else {}
    outcomes: List[str] = []
    for label in sorted((k for k in extra if "outcome" in k.lower()), key=lambda k: "title" not in k.lower()):
        outcomes += split_outcomes(extra[label])
    hours = ", ".join(f"{k}: {extra[k]}" for k in HOURS_LABELS if extra.get(k))
    return {"summary": course.get("description"), "outcomes": outcomes, "contact_hours": hours or None}


def save_document(conn, course_ids: Iterable[int], source_url: str, source_kind: str, doc: dict,
                  match_confidence: float, match_method: str, parse_method: str = "deterministic",
                  source_type: Optional[str] = "html") -> int:
    """Insert or replace one syllabus document (keyed by URL) and its sections, and link it to courses.
    `doc` may hold: summary, instructor, term, year, contact_hours, grading_scale, raw_text, extra, and the
    lists outcomes (str), topics ({week, title, details}), assessments ({name, kind, weight_pct, details}),
    materials ({kind, title, authors, required})."""
    sid = conn.execute(
        "INSERT INTO syllabi(source_url, source_kind, source_type, term, year, instructor, contact_hours, "
        "delivery_mode, summary, grading_scale, raw_text, parse_method, extra_json, extracted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(source_url) DO UPDATE SET "
        "source_kind=excluded.source_kind, source_type=excluded.source_type, term=excluded.term, year=excluded.year, "
        "instructor=excluded.instructor, contact_hours=excluded.contact_hours, delivery_mode=excluded.delivery_mode, "
        "summary=excluded.summary, grading_scale=excluded.grading_scale, raw_text=excluded.raw_text, "
        "parse_method=excluded.parse_method, extra_json=excluded.extra_json, extracted_at=excluded.extracted_at "
        "RETURNING id",
        (source_url, source_kind, source_type, doc.get("term"), doc.get("year"), doc.get("instructor"),
         doc.get("contact_hours"), doc.get("delivery_mode"), doc.get("summary"), doc.get("grading_scale"),
         doc.get("raw_text"), parse_method, json.dumps(doc["extra"]) if doc.get("extra") else None)).fetchone()[0]
    for t in ("syllabus_outcomes", "syllabus_topics", "syllabus_assessments", "syllabus_materials"):
        conn.execute(f"DELETE FROM {t} WHERE syllabus_id = ?", (sid,))
    conn.executemany("INSERT INTO syllabus_outcomes(syllabus_id, position, text) VALUES (?,?,?)",
                     [(sid, i, o) for i, o in enumerate(doc.get("outcomes") or [], 1)])
    conn.executemany("INSERT INTO syllabus_topics(syllabus_id, position, week, title, details) VALUES (?,?,?,?,?)",
                     [(sid, i, t.get("week"), t["title"], t.get("details"))
                      for i, t in enumerate(doc.get("topics") or [], 1)])
    conn.executemany("INSERT INTO syllabus_assessments(syllabus_id, name, kind, weight_pct, details) VALUES (?,?,?,?,?)",
                     [(sid, a["name"], a.get("kind"), a.get("weight_pct"), a.get("details"))
                      for a in doc.get("assessments") or []])
    conn.executemany("INSERT INTO syllabus_materials(syllabus_id, kind, title, authors, required) VALUES (?,?,?,?,?)",
                     [(sid, m.get("kind"), m["title"], m.get("authors"), m.get("required"))
                      for m in doc.get("materials") or []])
    for cid in course_ids:
        conn.execute("INSERT INTO syllabus_courses(syllabus_id, course_id, match_confidence, match_method) "
                     "VALUES (?,?,?,?) ON CONFLICT(syllabus_id, course_id) DO UPDATE SET "
                     "match_confidence=excluded.match_confidence, match_method=excluded.match_method",
                     (sid, cid, match_confidence, match_method))
    return sid


def load_catalog(conn, course_ids: Optional[Iterable[int]] = None) -> int:
    """Store each AI course's catalog entry as its first syllabus document. Returns how many were saved
    (the caller commits). Courses described inline on a program page (no course URL) use the program page
    URL + '#code'."""
    q = ("SELECT c.*, (SELECT p.source_url FROM program_courses pc JOIN programs p ON p.id = pc.program_id "
         "WHERE pc.course_id = c.id LIMIT 1) AS program_url FROM courses c WHERE c.is_ai = 1")
    rows = [dict(r) for r in conn.execute(q)]
    if course_ids is not None:
        wanted = set(course_ids)
        rows = [r for r in rows if r["id"] in wanted]
    n = 0
    for c in rows:
        url = c["source_url"] or f"{c['program_url'] or 'catalog'}#{c['code']}"
        save_document(conn, [c["id"]], url, "catalog", catalog_document(c), 1.0, "catalog")
        n += 1
    return n
