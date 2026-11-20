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


TERM_RE = re.compile(r"\b(spring|summer|fall|winter|autumn)\s*(?:semester|quarter|term)?\s*'?(20\d\d)\b", re.I)
INSTRUCTOR_RE = re.compile(r"^(?:instructors?|professor|lecturer)\s*:?\s*(.*)$", re.I)
NOT_A_NAME_RE = re.compile(r"office|hours|e-?mail|phone|tba|information|contact", re.I)  # labels, not names
OUTCOME_HEAD_RE = re.compile(r"outcomes|objectives|you will be able to|students will be able to", re.I)
BULLET_RE = re.compile(r"^\s*(?:[•●▪◦\-*–]|\d{1,2}[.)])\s+")
WEIGHT_RE = re.compile(r"([A-Za-z][A-Za-z0-9 &/'-]{0,40}?)\s*[:\-–(]?\s*(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%")
WEEK_RE = re.compile(r"^(?:week|wk\.?)\s*(\d{1,2})\b\s*[:.\-–)]*\s*(.{3,200})$", re.I)


def parse_text(text: str) -> dict:
    """Deterministic pass over a syllabus found on the web (any layout). Takes only what it can read
    reliably; the rest stays in raw_text for an LLM pass."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    doc: dict = {"outcomes": [], "topics": [], "assessments": [], "materials": [], "raw_text": "\n".join(lines)}
    m = TERM_RE.search("\n".join(lines[:60]))
    if m:
        doc["term"] = "Fall" if m.group(1).lower() == "autumn" else m.group(1).title()
        doc["year"] = int(m.group(2))
    for i, line in enumerate(lines[:80]):
        m = INSTRUCTOR_RE.match(line)
        if m:
            name = (m.group(1) or (lines[i + 1] if i + 1 < len(lines) else "")).strip(" ,;")
            if 2 < len(name) <= 80 and "@" not in name and not NOT_A_NAME_RE.search(name):
                doc["instructor"] = name
                break
    for i, line in enumerate(lines):
        if len(line) < 150 and OUTCOME_HEAD_RE.search(line):
            doc["outcomes"] += _bullets_after(lines, i)
    doc["outcomes"] = list(dict.fromkeys(doc["outcomes"]))[:25]
    doc["assessments"] = weights(lines)
    for line in lines:
        m = WEEK_RE.match(line)
        if m:
            doc["topics"].append({"week": m.group(1), "title": m.group(2)})
    return doc


def assessment_kind(name: str) -> str:
    n = name.lower()
    for kind, words in (("exam", ("exam", "midterm", "final", "test")), ("project", ("project", "capstone")),
                        ("quiz", ("quiz",)), ("participation", ("participation", "attendance", "discussion")),
                        ("homework", ("homework", "assignment", "lab", "problem set"))):
        if any(w in n for w in words):
            return kind
    return "other"


def _bullets_after(lines: List[str], start: int) -> List[str]:
    """Bullet items following a heading, joining lines a PDF wrapped (they start lowercase)."""
    items: List[str] = []
    for line in lines[start + 1:start + 60]:
        if BULLET_RE.match(line):
            items.append(BULLET_RE.sub("", line))
        elif items and line[:1].islower():
            items[-1] += " " + line
        elif items or len(line) > 150:
            break
    return [x for x in items if 10 <= len(x) <= 400]


def weights(lines: List[str]) -> List[dict]:
    """'Homework 40%' / 'Midterm: 25%' pairs, kept only if they add up to about 100 (so a letter-grade
    scale like 'A 90%' is never mistaken for weights). A name repeated (same table shown twice) counts once."""
    found, seen = [], set()
    for line in lines:
        for name, w in WEIGHT_RE.findall(line):
            name = name.strip(" -")
            if re.fullmatch(r"[A-F][+-]?|total|grade|above|below", name, re.I) or name.lower() in seen:
                continue
            seen.add(name.lower())
            found.append({"name": name, "kind": assessment_kind(name), "weight_pct": float(w)})
    total = sum(a["weight_pct"] for a in found)
    return found if found and 90 <= total <= 110 else []


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
