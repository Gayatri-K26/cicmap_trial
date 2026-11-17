"""Turn prerequisite text into AND-of-OR groups.

Output per course: {"prereq": [[option, ...], ...], "coreq": [...], "conditions": [str, ...]}
where each inner list is a set of alternatives (OR) and the outer list is all required (AND).
An option is {"code": str, "min_grade": str|None, "concurrent_ok": bool}.
"""
import json
import re
from typing import Dict, List, Optional

from .llm import LLM, LLMError
from .text import find_codes, normalize_code

GRADE_RE = re.compile(r"(?:min(?:imum)?\.?\s*grade(?:\s*of)?|grade\s*of)\s*:?\s*([A-DFS][+-]?)", re.I)


def regex_parse(text: Optional[str]) -> List[List[dict]]:
    """Best-effort fallback: parenthesized chunks or ' and '-separated parts are AND groups, codes inside are ORs."""
    if not text:
        return []
    chunks = re.findall(r"\(([^()]*)\)", text)
    rest = re.sub(r"\([^()]*\)", " ", text)
    chunks += re.split(r"\band\b|;|,", rest, flags=re.I)
    groups = []
    for ch in chunks:
        grade = GRADE_RE.search(ch)
        opts = [{"code": c, "min_grade": grade.group(1) if grade else None, "concurrent_ok": False}
                for c in find_codes(ch)]
        if opts:
            groups.append(opts)
    return groups


def parse_batch(llm: LLM, courses: Dict[str, Dict[str, Optional[str]]]) -> Dict[str, dict]:
    """courses: code -> {"prereq_text", "coreq_text"}. Returns code -> parsed structure + 'method'."""
    out = {code: {"prereq": regex_parse(t.get("prereq_text")), "coreq": regex_parse(t.get("coreq_text")),
                  "conditions": [], "method": "regex"}
           for code, t in courses.items()}
    todo = {c: t for c, t in courses.items() if t.get("prereq_text") or t.get("coreq_text")}
    if not llm.enabled or not todo:
        return out
    system = ("You convert university course prerequisite text into structured data. Answer with JSON only. "
              "Represent requirements as AND-of-OR groups: the outer list is all required, each inner list holds "
              "alternatives where any one satisfies it. Normalize course codes as 'SUBJ 123' (e.g. 'CPS*2800' -> "
              "'CPS 2800'). Put non-course requirements (class standing, major restrictions, permission, test "
              "scores) in 'conditions' as short strings. Mark concurrent_ok true when the text says the course "
              "may be taken concurrently.")
    user = (f"{json.dumps(todo, ensure_ascii=False)}\n\n"
            'Return {"courses": {"<code>": {"prereq": [[{"code": str, "min_grade": str|null, '
            '"concurrent_ok": bool}]], "coreq": [[...same...]], "conditions": [str]}}} for every code.')
    try:
        data = llm.json("parse_prereqs", 1, system, user, max_tokens=6000)
        for code, parsed in (data.get("courses") or {}).items():
            if code not in out or not isinstance(parsed, dict):
                continue
            out[code] = {"prereq": _clean_groups(parsed.get("prereq")),
                         "coreq": _clean_groups(parsed.get("coreq")),
                         "conditions": [str(c)[:300] for c in parsed.get("conditions") or [] if c],
                         "method": "llm"}
    except LLMError:
        pass
    return out


def _clean_groups(groups) -> List[List[dict]]:
    clean = []
    for g in groups or []:
        if isinstance(g, dict):
            g = [g]
        opts = []
        for o in g or []:
            code = normalize_code(str((o or {}).get("code", "")))
            if code:
                opts.append({"code": code, "min_grade": (o.get("min_grade") or None),
                             "concurrent_ok": bool(o.get("concurrent_ok"))})
        if opts:
            clean.append(opts)
    return clean
