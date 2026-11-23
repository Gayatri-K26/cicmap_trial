"""Find syllabus candidates on each school's own website with Brave Search.

One query per AI course, restricted to the school's domain (open web search mostly returns other schools'
courses with the same number). Results are cached in raw_pages, so re-runs make no new searches. Pages we
can't or shouldn't read are dropped: catalog pages (already used), syllabus portals handled elsewhere or
closed to crawlers, and LMS pages behind a login.
"""
import json
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from . import config
from .text import find_codes

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
MIN_INTERVAL = 0.2  # seconds between searches; the plan allows far more, no need to hurry
SKIP_HOST_PARTS = ("smartcatalogiq.com", "coursedog.com", "simplesyllabus.com", "campusconcourse.com",
                   "instructure.com", "brightspace", "d2l.", "blackboard", "canvas.", "mycourses", "login.")
SKIP_HOST_PREFIXES = ("catalog.", "programs.", "bulletin.")

_last = 0.0


def school_domain(catalog_host: str) -> str:
    """'catalog.purdue.edu' -> 'purdue.edu'; SmartCatalog's 'kean.smartcatalogiq.com' -> 'kean.edu'."""
    parts = catalog_host.lower().split(".")
    if catalog_host.lower().endswith("smartcatalogiq.com"):
        return f"{parts[0]}.edu"
    return ".".join(parts[-2:])


def queries_for(domain: str, code: str) -> List[str]:
    """One query per course. Broader ones ('course outline', the bare code) were tried on 2026-11-20: they
    added only course-site subpages (assignments, slides) and list pages, and no syllabus for the schools
    that had none, so they were dropped."""
    return [f'site:{domain} "{code}" syllabus']


def brave(conn, query: str, count: int = 10) -> List[dict]:
    """Search results [{url, title, description}], cached by query."""
    global _last
    key = f"brave:{query}"
    row = conn.execute("SELECT body FROM raw_pages WHERE url = ?", (key,)).fetchone()
    if row:
        return json.loads(row["body"])
    if not config.BRAVE_API_KEY:
        raise RuntimeError("BRAVE_API_KEY is not set in .env")
    wait = MIN_INTERVAL - (time.time() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.time()
    resp = httpx.get(BRAVE_URL, params={"q": query, "count": count}, timeout=30,
                     headers={"X-Subscription-Token": config.BRAVE_API_KEY, "Accept": "application/json"})
    resp.raise_for_status()
    results = [{"url": r.get("url"), "title": r.get("title"), "description": r.get("description")}
               for r in (resp.json().get("web") or {}).get("results", []) if r.get("url")]
    conn.execute("INSERT OR REPLACE INTO raw_pages(url, status_code, body, fetch_method) VALUES (?, 200, ?, 'search')",
                 (key, json.dumps(results)))
    conn.commit()
    return results


def usable(url: str, domain: str) -> bool:
    host = urlparse(url).netloc.lower()
    on_school_site = host == domain or host.endswith("." + domain)
    return (on_school_site and not host.startswith(SKIP_HOST_PREFIXES)
            and not any(p in host for p in SKIP_HOST_PARTS))


def candidates(conn, institution: Optional[str] = None) -> List[dict]:
    """Every usable search result per AI course: [{course_id, institution, code, title, domain, url, ...}]."""
    rows = conn.execute(
        "SELECT c.id, c.code, c.title, i.name AS institution, i.domain FROM courses c "
        "JOIN institutions i ON i.id = c.institution_id WHERE c.is_ai = 1 AND i.domain IS NOT NULL "
        "AND (:inst IS NULL OR i.name LIKE :inst) ORDER BY i.name, c.code",
        {"inst": f"%{institution}%" if institution else None}).fetchall()
    out: List[dict] = []
    for c in rows:
        domain = school_domain(c["domain"])
        seen = set()
        for q in queries_for(domain, c["code"]):
            for r in brave(conn, q):
                if usable(r["url"], domain) and r["url"] not in seen:
                    seen.add(r["url"])
                    out.append({"course_id": c["id"], "institution": c["institution"], "code": c["code"],
                                "course_title": c["title"], "domain": domain, **r})
    return out


# --- is this page a syllabus of this course? ----------------------------------------------------------

# URL words of pages that are never one course's syllabus; skipped before downloading.
NOT_SYLLABUS_URL = re.compile(r"catalog|bulletin|transfer|directory|/jobs|schedule|flowchart|rhodemap|"
                              r"audit|study-?plan|program-review|self-study|"
                              # pages of a course site other than its syllabus or home page
                              r"assignment|project|homework|/hw\d|lecture|slides|week\d|-w\d+-", re.I)
SIGNALS = [r"grading|grade (distribution|breakdown)", r"textbook|required (text|materials)|course materials",
           r"office hours", r"learning outcomes|course objectives|student learning|you will be able to",
           r"schedule|week \d|calendar", r"prerequisite", r"attendance", r"exam|midterm", r"homework|assignment",
           r"instructor|professor"]
MIN_SIGNALS = 3


def skip_url(url: str) -> bool:
    """Never a syllabus by its URL, unless the URL says 'syllabus' ('capstone-project-syllabus.pdf')."""
    return bool(NOT_SYLLABUS_URL.search(url)) and not re.search(r"syllab", url, re.I)
MAX_CODES = 15  # list pages name 17+ courses; the syllabi found so far name at most 12


def code_in(code: str, text: str) -> bool:
    """'CS 380' in 'CS380_Syllabus.pdf', 'CS-380', 'cs 380' (but not 'CS 3801')."""
    subj, num = code.split(" ", 1)
    return re.search(rf"\b{re.escape(subj)}[\s_\-*]*{re.escape(num)}(?!\d)", text, re.I) is not None


def assess(text: str, code: str, course_title: Optional[str], url: str,
           page_title: Optional[str] = "") -> Optional[Tuple[float, str]]:
    """(match confidence, method) if `text` reads like this course's syllabus, else None."""
    if not code_in(code, f"{page_title} {url} {text[:2000]}"):
        return None  # the course must be what the page is about, not a passing mention
    if len(set(find_codes(text))) > MAX_CODES:
        return None
    if sum(bool(re.search(s, text, re.I)) for s in SIGNALS) < MIN_SIGNALS:
        return None
    if course_title and course_title.lower() in text.lower():
        return 0.9, "code_and_title_in_text"
    return 0.75, "code_in_text"


def source_kind(url: str) -> str:
    """Instructor pages ('/~name/', '/homes/name/') vs department or office pages."""
    return "faculty" if re.search(r"/~|/homes/|/people/|/users/|/faculty/", url, re.I) else "department"


def discover(conn, institution: Optional[str] = None, progress=lambda m: None) -> dict:
    """Search, fetch, keep real syllabi, parse them deterministically and store them."""
    from . import documents, syllabi

    counts = {"candidates": 0, "fetched": 0, "kept": 0, "courses": set(), "removed": 0}
    found = candidates(conn, institution)
    kept_urls = set()
    for f in found:
        if skip_url(f["url"]):
            continue
        counts["candidates"] += 1
        try:
            text, kind = documents.fetch_text(conn, f["url"])
        except Exception as e:  # 404, robots.txt, bot check, unreadable file: skip this result
            progress(f"skip {f['url']}: {type(e).__name__}")
            continue
        counts["fetched"] += 1
        verdict = assess(text, f["code"], f["course_title"], f["url"], f["title"])
        if not verdict:
            continue
        doc = syllabi.parse_text(text)
        syllabi.save_document(conn, [f["course_id"]], f["url"], source_kind(f["url"]), doc, verdict[0], verdict[1],
                              source_type=kind)
        conn.commit()
        counts["kept"] += 1
        kept_urls.add(f["url"])
        counts["courses"].add(f["course_id"])
        progress(f"{f['institution']} {f['code']}: {f['url']}")
    # Web documents stored earlier for these courses that no longer pass (rules tightened) are removed.
    course_ids = {f["course_id"] for f in found}
    stale = [r["id"] for r in conn.execute(
        "SELECT DISTINCT s.id, s.source_url FROM syllabi s JOIN syllabus_courses sc ON sc.syllabus_id = s.id "
        "WHERE s.source_kind IN ('faculty', 'department')").fetchall()
        if r["source_url"] not in kept_urls and _linked_to(conn, r["id"], course_ids)]
    for sid in stale:
        conn.execute("DELETE FROM syllabi WHERE id = ?", (sid,))
    conn.commit()
    counts["removed"] = len(stale)
    counts["courses"] = len(counts["courses"])
    return counts


def _linked_to(conn, syllabus_id: int, course_ids: set) -> bool:
    return any(r[0] in course_ids for r in conn.execute(
        "SELECT course_id FROM syllabus_courses WHERE syllabus_id = ?", (syllabus_id,)))


def summary(found: List[dict]) -> Dict[str, Dict[str, int]]:
    """Per school: courses with at least one candidate, and candidate count."""
    by: Dict[str, Dict[str, set]] = {}
    for f in found:
        s = by.setdefault(f["institution"], {"courses": set(), "urls": set()})
        s["courses"].add(f["code"])
        s["urls"].add(f["url"])
    return {k: {"courses": len(v["courses"]), "candidates": len(v["urls"])} for k, v in sorted(by.items())}
