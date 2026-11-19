"""Extraction pipeline for one program source.

fetch page -> pick parser -> parse curriculum -> fill metadata gaps -> screen titles for AI
-> fetch details for candidates -> final AI classification -> parse prerequisites -> store.
"""
import json
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from . import db
from .adapters import select_adapter
from .adapters.llm_fallback import TEMP_PREFIX, LLMFallbackAdapter
from .browser import BrowserSession
from .ai_filter import classify_details, screen_titles
from .fetch import BlockedError, Fetcher
from .llm import LLM, LLMError
from .models import CourseData, CurriculumEntry, ProgramData
from .prereqs import parse_batch
from .text import guess_degree, split_code

log = logging.getLogger("cicmap")


@dataclass
class RunResult:
    source_id: int
    status: str
    message: str
    adapter: Optional[str] = None
    ai_courses: int = 0
    curriculum_courses: int = 0
    llm_calls: int = 0
    pages_fetched: int = 0
    browser_fetches: int = 0


def extract_source(conn, source_id: int, use_llm: bool = True, refresh: bool = False,
                   progress: Callable[[str], None] = lambda m: None, use_browser: bool = True,
                   browser: Optional[BrowserSession] = None) -> RunResult:
    """Extract one program. Pass a shared `browser` to reuse one headless browser across many programs."""
    src = conn.execute("SELECT * FROM program_sources WHERE id = ?", (source_id,)).fetchone()
    if not src:
        raise ValueError(f"No program source with id {source_id}")
    run_id = conn.execute("INSERT INTO extraction_runs(source_id, status) VALUES (?, 'running')",
                          (source_id,)).lastrowid
    conn.commit()
    fetcher = Fetcher(conn, refresh=refresh, use_browser=use_browser, browser=browser)
    llm = LLM(conn, enabled=use_llm)
    adapter_name = None
    try:
        progress(f"Fetching {src['catalog_url']}")
        html = fetcher.get(src["catalog_url"])

        page_url = src["catalog_url"]
        adapter, why = _pick_adapter(src, page_url, html, llm, fetcher)
        if not adapter:
            return _finish(conn, run_id, src, "needs_parser", f"No parser for this page ({why})", fetcher, llm)
        adapter_name = adapter.name
        progress(f"Parser: {adapter.name} ({why})")

        program = adapter.parse_program(page_url, html)
        if program.curriculum_url:
            # The page only summarizes the curriculum; follow its link to the full list once.
            page_url = program.curriculum_url
            progress(f"Following full course list: {page_url}")
            html = fetcher.get(page_url)
            summary = program
            adapter, why = _pick_adapter(src, page_url, html, llm, fetcher)
            if not adapter:
                return _finish(conn, run_id, src, "needs_parser", f"No parser for {page_url} ({why})",
                               fetcher, llm, adapter_name)
            adapter_name = adapter.name
            progress(f"Parser: {adapter.name} ({why})")
            program = adapter.parse_program(page_url, html)
            program.curriculum_url = None
            for f in ("degree", "degree_full", "college", "department", "catalog_year", "total_credits",
                      "description"):
                if not getattr(program, f):
                    setattr(program, f, getattr(summary, f))
        if not program.entries:
            return _finish(conn, run_id, src, "needs_parser",
                           f"Parser '{adapter.name}' found no courses; page layout may have changed",
                           fetcher, llm, adapter_name)
        _fill_metadata(llm, src, program)
        progress(f"Curriculum: {len(program.entries)} courses; screening for AI")

        labels = screen_titles(llm, src["program"], program.entries)
        candidates = [e for e in program.entries if labels.get(e.code) in ("yes", "maybe")]
        progress(f"{len(candidates)} candidates; fetching course details")

        details: List[CourseData] = []
        entries = {e.code: e for e in program.entries}
        for e in candidates:
            c = _course_details(adapter, fetcher, page_url, e)
            if c.code.startswith(TEMP_PREFIX):
                log.warning("no course code found for %s (%s)", e.title, e.detail_url)
                continue
            if c.code != e.code:  # code learned from the course page (e.g. BYU)
                entries[c.code] = entries.pop(e.code)
            details.append(c)
        verdicts = classify_details(llm, src["program"], details)
        ai_courses = [c for c in details if verdicts[c.code]["is_ai"]]
        progress(f"{len(ai_courses)} AI courses; parsing prerequisites")

        parsed = parse_batch(llm, {c.code: {"prereq_text": c.prereq_text, "coreq_text": c.coreq_text}
                                   for c in ai_courses})
        titles = {code: e.title for code, e in entries.items() if not code.startswith(TEMP_PREFIX)}
        _store(conn, src, program, adapter, ai_courses, verdicts, parsed, titles, entries)
        msg = f"{len(ai_courses)} AI courses out of {len(program.entries)} in the curriculum"
        res = _finish(conn, run_id, src, "extracted", msg, fetcher, llm, adapter_name)
        res.ai_courses, res.curriculum_courses = len(ai_courses), len(program.entries)
        return res
    except BlockedError as e:
        return _finish(conn, run_id, src, "blocked", str(e), fetcher, llm, adapter_name)
    except Exception as e:  # keep going with other schools; the error is recorded
        log.exception("extraction failed")
        conn.rollback()
        return _finish(conn, run_id, src, "failed", f"{type(e).__name__}: {e}", fetcher, llm, adapter_name)
    finally:
        fetcher.close()


def _pick_adapter(src, url: str, html: str, llm: LLM, fetcher: Fetcher):
    """Deterministic parser if one matches; otherwise the LLM fallback when the LLM is on."""
    adapter, why = select_adapter(url, html, src["adapter_override"])
    if adapter is None and llm.enabled:
        adapter, why = LLMFallbackAdapter(), "LLM fallback (no deterministic parser matched)"
    if isinstance(adapter, LLMFallbackAdapter):
        adapter.llm, adapter.institution, adapter.program = llm, src["institution"], src["program"]
        adapter.fetcher = fetcher
    return adapter, why


def _course_details(adapter, fetcher: Fetcher, program_url: str, e: CurriculumEntry) -> CourseData:
    try:
        inline = adapter.inline_course(e)
        if inline:
            inline.title = inline.title or e.title
            inline.credits_text = inline.credits_text or e.credits_text
            return inline
    except Exception as ex:
        log.warning("inline course details failed for %s: %s", e.code, ex)
    url = adapter.course_detail_url(program_url, e)
    if not url:
        return CourseData(code=e.code, title=e.title, credits_text=e.credits_text)
    try:
        c = adapter.parse_course(e, url, fetcher.get(url))
        c.title = c.title or e.title
        c.credits_text = c.credits_text or e.credits_text
        return c
    except Exception as ex:  # one bad course page shouldn't sink the program
        log.warning("course detail failed for %s: %s", e.code, ex)
        return CourseData(code=e.code, title=e.title, credits_text=e.credits_text, source_url=url)


def _fill_metadata(llm: LLM, src, p: ProgramData) -> None:
    if not p.degree:
        p.degree, p.degree_full = guess_degree(src["program"], p.name)
    if all([p.degree, p.college, p.department, p.catalog_year]) or not llm.enabled:
        return
    system = "You extract university program metadata. Answer with JSON only; use null when unknown."
    user = (f"Institution: {src['institution']}\nProgram (from our list): {src['program']}\n"
            f"Page title: {p.name}\nURL: {src['catalog_url']}\nPage header text:\n{(p.header_text or '')[:3000]}\n\n"
            'Return {"degree": short like "BS", "degree_full": str, "college": str, "department": str, '
            '"catalog_year": like "2026-2027", "total_credits": str}.')
    try:
        data = llm.json("program_metadata", 1, system, user, max_tokens=500)
    except LLMError:
        return
    for f in ("degree", "degree_full", "college", "department", "catalog_year", "total_credits"):
        if not getattr(p, f) and data.get(f):
            setattr(p, f, str(data[f])[:200])


def _store(conn, src, program: ProgramData, adapter, ai_courses: List[CourseData], verdicts: Dict[str, dict],
           parsed: Dict[str, dict], titles: Dict[str, Optional[str]], entries: Dict[str, CurriculumEntry]) -> None:
    inst_id = db.get_or_create_institution(conn, src["institution"], urlparse(src["catalog_url"]).netloc)
    for f in ("college", "department"):  # breadcrumb separators like "College of X ·"
        v = getattr(program, f)
        if v:
            setattr(program, f, v.strip(" ·|>/-–—") or None)
    conn.execute(
        "INSERT INTO programs(source_id, institution_id, name, degree, degree_full, college, department, "
        "catalog_year, total_credits, description, source_url, adapter, adapter_version, extracted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(source_id) DO UPDATE SET "
        "institution_id=excluded.institution_id, name=excluded.name, degree=excluded.degree, "
        "degree_full=excluded.degree_full, college=excluded.college, department=excluded.department, "
        "catalog_year=excluded.catalog_year, total_credits=excluded.total_credits, "
        "description=excluded.description, source_url=excluded.source_url, adapter=excluded.adapter, "
        "adapter_version=excluded.adapter_version, extracted_at=excluded.extracted_at",
        (src["id"], inst_id, src["program"] or program.name, program.degree, program.degree_full, program.college,
         program.department, program.catalog_year, program.total_credits, program.description,
         src["catalog_url"], adapter.name, adapter.version))
    program_id = conn.execute("SELECT id FROM programs WHERE source_id = ?", (src["id"],)).fetchone()["id"]
    conn.execute("DELETE FROM program_courses WHERE program_id = ?", (program_id,))

    for c in ai_courses:
        v = verdicts[c.code]
        subj, num = split_code(c.code)
        conn.execute(
            "INSERT INTO courses(institution_id, code, subject, number, title, credits_min, credits_max, "
            "credits_text, description, prereq_text, coreq_text, restrictions, extra_json, source_url, is_ai, "
            "is_stub, ai_confidence, ai_category, ai_reason, ai_method, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,?,?,?,datetime('now')) "
            "ON CONFLICT(institution_id, code) DO UPDATE SET title=excluded.title, "
            "credits_min=excluded.credits_min, credits_max=excluded.credits_max, credits_text=excluded.credits_text, "
            "description=excluded.description, prereq_text=excluded.prereq_text, coreq_text=excluded.coreq_text, "
            "restrictions=excluded.restrictions, extra_json=excluded.extra_json, source_url=excluded.source_url, "
            "is_ai=1, is_stub=0, ai_confidence=excluded.ai_confidence, ai_category=excluded.ai_category, "
            "ai_reason=excluded.ai_reason, ai_method=excluded.ai_method, updated_at=excluded.updated_at",
            (inst_id, c.code, subj, num, c.title, c.credits_min, c.credits_max, c.credits_text, c.description,
             c.prereq_text, c.coreq_text, c.restrictions, json.dumps(c.extra) if c.extra else None, c.source_url,
             v["confidence"], v["category"], v["reason"], v["method"]))
        course_id = _course_id(conn, inst_id, c.code)
        e = entries.get(c.code)
        conn.execute("INSERT OR REPLACE INTO program_courses(program_id, course_id, requirement_group, "
                     "is_alternative) VALUES (?,?,?,?)",
                     (program_id, course_id, e.requirement_group if e else None, int(e.is_alternative) if e else 0))

        conn.execute("DELETE FROM course_prereqs WHERE course_id = ?", (course_id,))
        p = parsed.get(c.code) or {}
        for kind in ("prereq", "coreq"):
            for gno, group in enumerate(p.get(kind) or [], start=1):
                for opt in group:
                    target = _ensure_course(conn, inst_id, opt["code"], titles.get(opt["code"]))
                    conn.execute("INSERT INTO course_prereqs(course_id, kind, group_no, prereq_course_id, min_grade, "
                                 "concurrent_ok, parse_method) VALUES (?,?,?,?,?,?,?)",
                                 (course_id, kind, gno, target, opt.get("min_grade"), int(opt.get("concurrent_ok")),
                                  p.get("method")))
        base = len(p.get("prereq") or [])
        for i, cond in enumerate(p.get("conditions") or [], start=1):
            conn.execute("INSERT INTO course_prereqs(course_id, kind, group_no, condition_text, parse_method) "
                         "VALUES (?, 'prereq', ?, ?, ?)", (course_id, base + i, cond, p.get("method")))


def _course_id(conn, inst_id: int, code: str) -> int:
    return conn.execute("SELECT id FROM courses WHERE institution_id = ? AND code = ?", (inst_id, code)).fetchone()["id"]


def _ensure_course(conn, inst_id: int, code: str, title: Optional[str]) -> int:
    row = conn.execute("SELECT id FROM courses WHERE institution_id = ? AND code = ?", (inst_id, code)).fetchone()
    if row:
        if title:
            conn.execute("UPDATE courses SET title = COALESCE(title, ?) WHERE id = ?", (title, row["id"]))
        return row["id"]
    subj, num = split_code(code)
    return conn.execute("INSERT INTO courses(institution_id, code, subject, number, title, is_ai, is_stub) "
                        "VALUES (?,?,?,?,?,0,1)", (inst_id, code, subj, num, title)).lastrowid


def _finish(conn, run_id, src, status, message, fetcher, llm, adapter=None) -> RunResult:
    conn.execute("UPDATE extraction_runs SET finished_at=datetime('now'), status=?, adapter=?, llm_calls=?, "
                 "pages_fetched=?, message=? WHERE id=?",
                 (status, adapter, llm.calls, fetcher.pages_fetched, message, run_id))
    conn.execute("UPDATE program_sources SET status=?, last_error=?, updated_at=datetime('now')"
                 + (", last_extracted_at=datetime('now')" if status == "extracted" else "") + " WHERE id=?",
                 (status, None if status == "extracted" else message, src["id"]))
    conn.commit()
    return RunResult(src["id"], status, message, adapter, llm_calls=llm.calls, pages_fetched=fetcher.pages_fetched,
                     browser_fetches=fetcher.browser_fetches)
