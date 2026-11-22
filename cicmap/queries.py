"""Read-side queries shared by the CLI and the UI. Everything returns plain dicts and lists."""
import json
import re
from typing import Dict, List, Optional, Tuple

from .syllabi import is_syllabus_label


def _rows(cur) -> List[dict]:
    return [dict(r) for r in cur]


def add_source(conn, institution: str, program: str, url: str) -> Tuple[int, bool]:
    """Track a program by hand. Returns (source id, already_tracked)."""
    url = url.strip()
    row = conn.execute("SELECT id FROM program_sources WHERE catalog_url = ?", (url,)).fetchone()
    if row:
        return row["id"], True
    sid = conn.execute("INSERT INTO program_sources(institution, program, catalog_url, origin) "
                       "VALUES (?, ?, ?, 'manual')", (institution.strip(), program.strip(), url)).lastrowid
    conn.commit()
    return sid, False


def sources(conn, include_inactive: bool = False) -> List[dict]:
    """Every tracked program with its status and AI-course count."""
    return _rows(conn.execute(
        "SELECT s.id, s.institution, s.program, s.catalog_url, s.origin, s.status, s.active, s.last_error, "
        "s.last_extracted_at, p.id AS program_id, p.adapter, "
        "(SELECT COUNT(*) FROM program_courses pc WHERE pc.program_id = p.id) AS ai_courses "
        "FROM program_sources s LEFT JOIN programs p ON p.source_id = s.id "
        + ("" if include_inactive else "WHERE s.active = 1 ") + "ORDER BY s.institution, s.program"))


def source(conn, source_id: int) -> Optional[dict]:
    rows = [s for s in sources(conn, include_inactive=True) if s["id"] == source_id]
    return rows[0] if rows else None


def program(conn, program_id: int) -> Optional[dict]:
    r = conn.execute("SELECT p.*, i.name AS institution FROM programs p "
                     "JOIN institutions i ON i.id = p.institution_id WHERE p.id = ?", (program_id,)).fetchone()
    return dict(r) if r else None


def ai_courses(conn, program_id: int) -> List[dict]:
    return _rows(conn.execute(
        "SELECT c.id, c.code, c.title, c.credits_text AS credits, c.ai_category AS category, "
        "ROUND(c.ai_confidence, 2) AS confidence, pc.requirement_group, pc.is_alternative, "
        "(SELECT COUNT(*) FROM syllabus_courses sc WHERE sc.course_id = c.id) AS syllabi "
        "FROM program_courses pc JOIN courses c ON c.id = pc.course_id "
        "WHERE pc.program_id = ? AND c.is_ai = 1 ORDER BY c.code", (program_id,)))


def prereq_groups(conn, course_id: int, kind: str = "prereq") -> List[List[str]]:
    """AND-of-OR groups as labels, e.g. [['CS 312', 'MATH 320'], ['junior standing']]."""
    groups: Dict[int, List[str]] = {}
    for r in conn.execute(
            "SELECT p.group_no, p.min_grade, p.concurrent_ok, p.condition_text, t.code FROM course_prereqs p "
            "LEFT JOIN courses t ON t.id = p.prereq_course_id WHERE p.course_id = ? AND p.kind = ? "
            "ORDER BY p.group_no, p.id", (course_id, kind)):
        label = r["code"] or r["condition_text"]
        if r["min_grade"]:
            label += f" (min {r['min_grade']})"
        if r["concurrent_ok"]:
            label += " (may be concurrent)"
        groups.setdefault(r["group_no"], []).append(label)
    return list(groups.values())


def format_groups(groups: List[List[str]]) -> str:
    return "  AND  ".join("(" + " OR ".join(g) + ")" if len(g) > 1 else g[0] for g in groups)


def prereq_cycles(conn, institution_id: Optional[int] = None) -> List[List[str]]:
    """Prerequisite loops (A needs B ... needs A), each as codes like ['CS 1', 'CS 2', 'CS 1'].
    A real curriculum has none, so a loop means a parsing or catalog error. Corequisites are left out:
    a lecture and its lab may each list the other."""
    edges: Dict[int, List[int]] = {}
    codes: Dict[int, str] = {}
    for r in conn.execute(
            "SELECT p.course_id, p.prereq_course_id, a.code AS a_code, b.code AS b_code FROM course_prereqs p "
            "JOIN courses a ON a.id = p.course_id JOIN courses b ON b.id = p.prereq_course_id "
            "WHERE p.kind = 'prereq' AND (:inst IS NULL OR a.institution_id = :inst)", {"inst": institution_id}):
        edges.setdefault(r["course_id"], []).append(r["prereq_course_id"])
        codes[r["course_id"]], codes[r["prereq_course_id"]] = r["a_code"], r["b_code"]
    cycles, state, path = [], {}, []

    def visit(u):
        state[u] = "open"
        path.append(u)
        for v in edges.get(u, []):
            if state.get(v) == "open":
                cycles.append([codes[x] for x in path[path.index(v):]] + [codes[v]])
            elif v not in state:
                visit(v)
        path.pop()
        state[u] = "done"

    for u in list(edges):
        if u not in state:
            visit(u)
    return cycles


def course(conn, course_id: int) -> Optional[dict]:
    r = conn.execute("SELECT c.*, i.name AS institution FROM courses c "
                     "JOIN institutions i ON i.id = c.institution_id WHERE c.id = ?", (course_id,)).fetchone()
    if not r:
        return None
    c = dict(r)
    extra = json.loads(c["extra_json"]) if c.get("extra_json") else {}
    # Labels the syllabus uses (outcomes, hours) are shown there, not repeated with the course details.
    c["extra"] = {k: v for k, v in extra.items() if not is_syllabus_label(k)}
    c["prereqs"] = prereq_groups(conn, course_id, "prereq")
    c["coreqs"] = prereq_groups(conn, course_id, "coreq")
    c["required_by"] = [x["code"] for x in conn.execute(
        "SELECT DISTINCT c2.code FROM course_prereqs p JOIN courses c2 ON c2.id = p.course_id "
        "WHERE p.prereq_course_id = ? ORDER BY c2.code", (course_id,))]
    return c


def find_course(conn, school: str, code: str) -> Optional[dict]:
    r = conn.execute("SELECT c.id FROM courses c JOIN institutions i ON i.id = c.institution_id "
                     "WHERE i.name LIKE ? AND c.code = ?", (f"%{school}%", code.upper())).fetchone()
    return course(conn, r["id"]) if r else None


def syllabi(conn, course_id: int) -> List[dict]:
    """Syllabus documents linked to a course, best first, each with its outcomes, topics, assessments and
    materials. Order: real syllabi before the catalog entry (they are more specific), then match confidence,
    then the most recent term."""
    out = []
    for s in _rows(conn.execute(
            "SELECT s.*, sc.match_confidence, sc.match_method FROM syllabus_courses sc "
            "JOIN syllabi s ON s.id = sc.syllabus_id WHERE sc.course_id = ? "
            "ORDER BY s.source_kind = 'catalog', sc.match_confidence DESC, s.year DESC, s.id", (course_id,))):
        sid = s["id"]
        s["outcomes"] = [r["text"] for r in conn.execute(
            "SELECT text FROM syllabus_outcomes WHERE syllabus_id = ? ORDER BY position", (sid,))]
        s["topics"] = _rows(conn.execute(
            "SELECT week, title, details FROM syllabus_topics WHERE syllabus_id = ? ORDER BY position", (sid,)))
        s["assessments"] = _rows(conn.execute(
            "SELECT name, kind, weight_pct, details FROM syllabus_assessments WHERE syllabus_id = ?", (sid,)))
        s["materials"] = _rows(conn.execute(
            "SELECT kind, title, authors, required FROM syllabus_materials WHERE syllabus_id = ?", (sid,)))
        out.append(s)
    return out


PROFILE_SECTIONS = ("summary", "outcomes", "topics", "assessments", "materials", "instructor", "contact_hours",
                    "grading_scale")


def syllabus_profile(conn, course_id: int) -> dict:
    """One combined syllabus per course: each section taken from the best document that has it
    (documents are already ordered best first), with its source, plus the sections nobody provided.
    The summary comes only from real syllabi: the catalog description is already the course description."""
    docs = syllabi(conn, course_id)
    sections = {}
    for name in PROFILE_SECTIONS:
        for d in docs:
            if name == "summary" and d["source_kind"] == "catalog":
                continue
            value = d.get(name)
            if value:
                sections[name] = {"value": value, "source": {k: d[k] for k in (
                    "id", "source_kind", "source_url", "term", "year", "match_confidence")}}
                break
    return {"sections": sections, "missing": [n for n in PROFILE_SECTIONS if n not in sections],
            "documents": docs}


def search_syllabi(conn, text: str) -> List[dict]:
    """Which schools teach a topic: AI courses whose title, summary, outcomes or weekly topics mention `text`.
    Returns one entry per school: {institution, courses: [{course_id, code, title, found_in: [...]}]}."""
    like = f"%{text.strip()}%"
    q = ("SELECT i.name AS institution, c.id AS course_id, c.code, c.title, x.section FROM ("
         " SELECT id AS course_id, 'title' AS section FROM courses WHERE title LIKE :q"
         " UNION SELECT sc.course_id, 'summary' FROM syllabi s JOIN syllabus_courses sc ON sc.syllabus_id = s.id"
         "  WHERE s.summary LIKE :q"
         " UNION SELECT sc.course_id, 'outcomes' FROM syllabus_outcomes o"
         "  JOIN syllabus_courses sc ON sc.syllabus_id = o.syllabus_id WHERE o.text LIKE :q"
         " UNION SELECT sc.course_id, 'topics' FROM syllabus_topics t"
         "  JOIN syllabus_courses sc ON sc.syllabus_id = t.syllabus_id WHERE t.title LIKE :q OR t.details LIKE :q"
         ") x JOIN courses c ON c.id = x.course_id JOIN institutions i ON i.id = c.institution_id "
         "WHERE c.is_ai = 1 ORDER BY i.name, c.code")
    schools: Dict[str, Dict[int, dict]] = {}
    for r in conn.execute(q, {"q": like}):
        course = schools.setdefault(r["institution"], {}).setdefault(
            r["course_id"], {"course_id": r["course_id"], "code": r["code"], "title": r["title"], "found_in": []})
        course["found_in"].append(r["section"])
    return [{"institution": k, "courses": list(v.values())} for k, v in schools.items()]


def _title_key(title: str) -> str:
    """'Artificial Intelligence: A Modern Approach (4th ed.)' -> 'artificial intelligence a modern approach'."""
    t = re.sub(r"\([^)]*\)", " ", title.lower())
    t = re.sub(r"\b(\d+(st|nd|rd|th)|edition|ed)\b", " ", t)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def common_materials(conn, min_schools: int = 2) -> List[dict]:
    """Textbooks and readings used at `min_schools` or more schools, most widely used first. Software
    ('Python', 'PyTorch') is left out: it would top every list without saying anything about content."""
    groups: Dict[str, dict] = {}
    for r in conn.execute(
            "SELECT m.title, m.authors, i.name AS institution, c.code FROM syllabus_materials m "
            "JOIN syllabus_courses sc ON sc.syllabus_id = m.syllabus_id JOIN courses c ON c.id = sc.course_id "
            "JOIN institutions i ON i.id = c.institution_id WHERE COALESCE(m.kind, '') != 'software'"):
        g = groups.setdefault(_title_key(r["title"]), {"title": r["title"], "authors": r["authors"],
                                                       "schools": {}})
        g["schools"].setdefault(r["institution"], set()).add(r["code"])
    out = [{"title": g["title"], "authors": g["authors"], "school_count": len(g["schools"]),
            "schools": {k: sorted(v) for k, v in sorted(g["schools"].items())}}
           for g in groups.values() if len(g["schools"]) >= min_schools]
    return sorted(out, key=lambda g: (-g["school_count"], g["title"]))


def totals(conn) -> dict:
    r = conn.execute(
        "SELECT (SELECT COUNT(*) FROM program_sources WHERE active = 1) AS programs, "
        "(SELECT COUNT(*) FROM program_sources WHERE active = 1 AND status = 'extracted') AS extracted, "
        "(SELECT COUNT(*) FROM courses WHERE is_ai = 1) AS ai_courses, "
        "(SELECT COUNT(*) FROM course_prereqs WHERE prereq_course_id IS NOT NULL) AS prereq_links, "
        "(SELECT COUNT(*) FROM syllabi) AS syllabi").fetchone()
    return dict(r)
