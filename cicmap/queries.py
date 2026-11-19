"""Read-side queries shared by the CLI and the UI. Everything returns plain dicts and lists."""
import json
from typing import Dict, List, Optional, Tuple


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
        "(SELECT COUNT(*) FROM syllabi s WHERE s.course_id = c.id) AS syllabi "
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


def course(conn, course_id: int) -> Optional[dict]:
    r = conn.execute("SELECT c.*, i.name AS institution FROM courses c "
                     "JOIN institutions i ON i.id = c.institution_id WHERE c.id = ?", (course_id,)).fetchone()
    if not r:
        return None
    c = dict(r)
    c["extra"] = json.loads(c["extra_json"]) if c.get("extra_json") else {}
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
    """Syllabi for a course, each with its outcomes, topics, assessments and materials."""
    out = []
    for s in _rows(conn.execute("SELECT * FROM syllabi WHERE course_id = ? ORDER BY year DESC, term",
                                (course_id,))):
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


def totals(conn) -> dict:
    r = conn.execute(
        "SELECT (SELECT COUNT(*) FROM program_sources WHERE active = 1) AS programs, "
        "(SELECT COUNT(*) FROM program_sources WHERE active = 1 AND status = 'extracted') AS extracted, "
        "(SELECT COUNT(*) FROM courses WHERE is_ai = 1) AS ai_courses, "
        "(SELECT COUNT(*) FROM course_prereqs WHERE prereq_course_id IS NOT NULL) AS prereq_links, "
        "(SELECT COUNT(*) FROM syllabi) AS syllabi").fetchone()
    return dict(r)
