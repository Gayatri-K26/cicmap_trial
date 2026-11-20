"""LLM pass over syllabus documents: fill the sections the deterministic parsers left empty.

Runs after discovery (`cicmap syllabus portal` / `web`). DeepSeek reads a document's raw_text and returns the
standard sections; only empty sections are filled, never ones the deterministic pass found. Guard rails
against made-up data: every outcome, topic, assessment and material title must appear in the document text,
and grading weights are kept only if they add up to about 100. Filled sections are listed in the document's
extra_json ('llm_sections') and its parse_method becomes 'deterministic+llm'. Answers are cached, so a re-run
(e.g. after discovery re-parses a document) costs nothing.
"""
import json
import re
from typing import List, Optional

from .llm import LLM, LLMError
from .syllabi import assessment_kind

MAX_CHARS = 15000
SYSTEM = ("You extract structured sections from a university course syllabus. Answer with JSON only. Copy text "
          "from the document; never invent items. Use empty lists or null when a section is absent.")
USER = """Course: {code} {title}
Syllabus text:
---
{text}
---
Return {{"outcomes": [learning outcome sentences], "topics": [{{"week": str or null, "title": str}}],
"assessments": [{{"name": str, "weight_pct": number or null}}], "materials": [{{"title": str, "authors": str or
null, "kind": "textbook" | "software" | "other", "required": true | false | null}}], "instructor": str or null,
"term": "Spring" | "Summer" | "Fall" | "Winter" | null, "year": int or null, "grading_scale": short str or null}}
Assessments are the graded components with their share of the final grade (e.g. Homework 40); if weights are
given in points, convert them to percent of the total. Materials are textbooks, readings and required software,
not campus services."""


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def in_text(item: str, text_norm: str) -> bool:
    """The item (or its first ~6 words, for long sentences the model may trim) appears in the document."""
    words = _norm(item).split()
    return bool(words) and " ".join(words[:6]) in text_norm


def checked(data: dict, text: str) -> dict:
    """Keep only what the document supports."""
    t = _norm(text)
    out: dict = {
        "outcomes": [str(o).strip() for o in data.get("outcomes") or [] if isinstance(o, str) and in_text(o, t)],
        "topics": [{"week": (str(x.get("week")) if x.get("week") is not None else None), "title": str(x["title"])}
                   for x in data.get("topics") or [] if isinstance(x, dict) and x.get("title")
                   and in_text(str(x["title"]), t)],
        "materials": [{"title": str(m["title"])[:300], "authors": m.get("authors"),
                       "kind": m.get("kind") if m.get("kind") in ("textbook", "software", "other") else "other",
                       "required": m.get("required") if isinstance(m.get("required"), bool) else None}
                      for m in data.get("materials") or [] if isinstance(m, dict) and m.get("title")
                      and in_text(str(m["title"]), t)],
    }
    assessments = []
    for a in data.get("assessments") or []:
        if isinstance(a, dict) and a.get("name") and in_text(str(a["name"]), t):
            w = a.get("weight_pct")
            w = float(w) if isinstance(w, (int, float)) and 0 < w <= 100 else None
            assessments.append({"name": str(a["name"])[:120], "kind": assessment_kind(str(a["name"])),
                                "weight_pct": w})
    weights = [a["weight_pct"] for a in assessments if a["weight_pct"] is not None]
    if weights and not 90 <= sum(weights) <= 110:  # inconsistent weights: keep the names, drop the numbers
        for a in assessments:
            a["weight_pct"] = None
    out["assessments"] = assessments
    for k in ("instructor", "grading_scale"):
        v = data.get(k)
        out[k] = str(v)[:300] if isinstance(v, str) and v.strip() and in_text(v, t) else None
    out["term"] = data.get("term") if data.get("term") in ("Spring", "Summer", "Fall", "Winter") else None
    y = data.get("year")
    out["year"] = y if isinstance(y, int) and 1990 <= y <= 2100 and str(y) in text else None
    return out


def fill(conn, llm: LLM, syllabus_id: int) -> List[str]:
    """Fill one document's empty sections. Returns the names of the sections filled."""
    s = conn.execute("SELECT * FROM syllabi WHERE id = ?", (syllabus_id,)).fetchone()
    if not s or not s["raw_text"]:
        return []
    course = conn.execute("SELECT c.code, c.title FROM syllabus_courses sc JOIN courses c ON c.id = sc.course_id "
                          "WHERE sc.syllabus_id = ? LIMIT 1", (syllabus_id,)).fetchone()
    try:
        data = llm.json("syllabus_sections", 1, SYSTEM, USER.format(
            code=course["code"] if course else "", title=(course["title"] if course else "") or "",
            text=s["raw_text"][:MAX_CHARS]), max_tokens=4000)
    except LLMError:
        return []
    got = checked(data, s["raw_text"])
    filled = []
    children = {"outcomes": ("syllabus_outcomes", "INSERT INTO syllabus_outcomes(syllabus_id, position, text) "
                                                  "VALUES (?,?,?)", lambda i, x: (syllabus_id, i, x)),
                "topics": ("syllabus_topics", "INSERT INTO syllabus_topics(syllabus_id, position, week, title) "
                                              "VALUES (?,?,?,?)", lambda i, x: (syllabus_id, i, x["week"], x["title"])),
                "assessments": ("syllabus_assessments", "INSERT INTO syllabus_assessments(syllabus_id, name, kind, "
                                "weight_pct) VALUES (?,?,?,?)",
                                lambda i, x: (syllabus_id, x["name"], x["kind"], x["weight_pct"])),
                "materials": ("syllabus_materials", "INSERT INTO syllabus_materials(syllabus_id, kind, title, "
                              "authors, required) VALUES (?,?,?,?,?)",
                              lambda i, x: (syllabus_id, x["kind"], x["title"], x["authors"], x["required"]))}
    for name, (table, sql, row) in children.items():
        has = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE syllabus_id = ?", (syllabus_id,)).fetchone()[0]
        if got[name] and not has:
            conn.executemany(sql, [row(i, x) for i, x in enumerate(got[name], 1)])
            filled.append(name)
        elif name == "assessments" and has and got[name]:
            # names found by the rules but no weights: take the model's weights for the same names
            missing = {r["id"]: _norm(r["name"]) for r in conn.execute(
                "SELECT id, name FROM syllabus_assessments WHERE syllabus_id = ? AND weight_pct IS NULL",
                (syllabus_id,))}
            llm_w = {_norm(a["name"]): a["weight_pct"] for a in got[name] if a["weight_pct"] is not None}
            hits = [(llm_w[n], i) for i, n in missing.items() if n in llm_w]
            known = conn.execute("SELECT COALESCE(SUM(weight_pct), 0) FROM syllabus_assessments "
                                 "WHERE syllabus_id = ? AND weight_pct IS NOT NULL", (syllabus_id,)).fetchone()[0]
            if hits and 90 <= known + sum(w for w, _ in hits) <= 110:  # only if the document then adds up
                conn.executemany("UPDATE syllabus_assessments SET weight_pct = ? WHERE id = ?", hits)
                filled.append("assessment weights")
    for col in ("instructor", "grading_scale", "term", "year"):
        if got[col] is not None and s[col] is None:
            conn.execute(f"UPDATE syllabi SET {col} = ? WHERE id = ?", (got[col], syllabus_id))
            filled.append(col)
    if filled:
        extra = json.loads(s["extra_json"]) if s["extra_json"] else {}
        extra["llm_sections"] = sorted(set(extra.get("llm_sections", [])) | set(filled))
        conn.execute("UPDATE syllabi SET parse_method = 'deterministic+llm', extra_json = ? WHERE id = ?",
                     (json.dumps(extra), syllabus_id))
    conn.commit()
    return filled


def fill_all(conn, llm: LLM, institution: Optional[str] = None, progress=lambda m: None) -> dict:
    """Run the LLM pass over every non-catalog syllabus document."""
    ids = [r["id"] for r in conn.execute(
        "SELECT DISTINCT s.id FROM syllabi s JOIN syllabus_courses sc ON sc.syllabus_id = s.id "
        "JOIN courses c ON c.id = sc.course_id JOIN institutions i ON i.id = c.institution_id "
        "WHERE s.source_kind != 'catalog' AND (:inst IS NULL OR i.name LIKE :inst) ORDER BY s.id",
        {"inst": f"%{institution}%" if institution else None})]
    counts = {"documents": len(ids), "changed": 0}
    for sid in ids:
        filled = fill(conn, llm, sid)
        if filled:
            counts["changed"] += 1
            progress(f"syllabus {sid}: {', '.join(filled)}")
    return counts
