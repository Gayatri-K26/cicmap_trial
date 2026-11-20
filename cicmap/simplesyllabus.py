"""Syllabi from public Simple Syllabus libraries (Purdue, Middle Georgia State).

Discovery uses the library's public search (JSON), then each syllabus page is loaded in the browser (the page
is a JavaScript app) and parsed deterministically: every section is a `div.component` with an <h2> title, and
titles are matched by keyword ("outcome", "material", "grading", ...). Anything not recognized stays in
raw_text for a later LLM pass. Kean and Miami also use Simple Syllabus, but their libraries need a login.
"""
import logging
import re
from typing import Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup

from . import config, syllabi
from .fetch import BlockedError, Fetcher, robots_allowed
from .text import clean, normalize_code

log = logging.getLogger("cicmap")

# institution name -> (library subdomain, text a course title must contain, or None)
PORTALS: Dict[str, Tuple[str, Optional[str]]] = {
    "Purdue University": ("purdue", "(WL)"),  # West Lafayette; Fort Wayne ('(FW)') reuses course codes
    "Middle Georgia State University": ("mga", None),
}
TERMS_PER_COURSE = 3  # newer syllabi can drop sections (e.g. textbooks), so keep a few recent terms
SEASONS = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


# --- discovery ----------------------------------------------------------------------------------------

def search(slug: str, code: str) -> List[dict]:
    url = f"https://{slug}.simplesyllabus.com/api2/doc-library-search"
    if not robots_allowed(url):
        raise BlockedError(f"robots.txt disallows {url}")
    resp = httpx.get(url, params={"search": code}, timeout=30,
                     headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    resp.raise_for_status()
    return resp.json().get("items") or []


def term_key(term_name: str) -> Tuple[int, int]:
    """'WL Fall 2026' -> (2026, 3), for sorting newest first."""
    m = re.search(r"(winter|spring|summer|fall)\s+(\d{4})", term_name or "", re.I)
    return (int(m.group(2)), SEASONS[m.group(1).lower()]) if m else (0, 0)


def pick(items: List[dict], code: str, marker: Optional[str]) -> List[dict]:
    """Public syllabi of exactly this course, one per term, newest first. 'CS (WL) 37300 PIN' -> 'CS 37300'."""
    by_term: Dict[str, dict] = {}
    for it in items:
        title = it.get("title") or ""
        if it.get("visibility") != "general_public" or (marker and marker not in title):
            continue
        if normalize_code(re.sub(r"\([^)]*\)", " ", title)) != code:
            continue
        by_term.setdefault(it.get("term_name") or "", it)  # sections of one term share a syllabus
    return sorted(by_term.values(), key=lambda it: term_key(it.get("term_name")), reverse=True)[:TERMS_PER_COURSE]


def match_confidence(item: dict, course_title: Optional[str]) -> float:
    """Exact code match is required; the title agreeing as well makes it near-certain."""
    a, b = (item.get("sub_title") or "").lower(), (course_title or "").lower()
    return 0.95 if a and b and (a in b or b in a) else 0.8


# --- parsing ------------------------------------------------------------------------------------------

_NO_TEXTBOOK = re.compile(r"^(no\b.*\brequired|see\b)", re.I)  # 'No textbooks required', 'See the details...'


def _lines(comp) -> List[str]:
    lines = [clean(x) for x in comp.get_text("\n", strip=True).split("\n")]
    return [x for x in lines[1:] if x]  # first line is the section title


def _materials(comp) -> List[dict]:
    """Each list item: a title line, then labeled fields ('Authors:', 'Edition:')."""
    out = []
    for li in comp.find_all("li"):
        lines = [clean(x) for x in li.get_text("\n", strip=True).split("\n") if clean(x)]
        if not lines or _NO_TEXTBOOK.match(lines[0]):
            continue
        fields = {lines[i].rstrip(":").lower(): lines[i + 1] for i in range(len(lines) - 1) if lines[i].endswith(":")}
        title, edition = lines[0], fields.get("edition")
        if edition and edition.lower() not in title.lower():  # 'AI: A Modern Approach (4th Edition)' has it
            title += f" ({edition})" if re.search(r"\bed", edition, re.I) else f" ({edition} ed.)"
        out.append({"kind": "textbook" if "isbn" in fields else "other", "title": title,
                    "authors": fields.get("authors")})
    return out


def parse_page(html: str) -> dict:
    """A rendered Simple Syllabus page -> the section dict syllabi.save_document takes."""
    soup = BeautifulSoup(html, "lxml")
    doc: dict = {"outcomes": [], "topics": [], "assessments": [], "materials": []}
    raw, weight_lines = [], []
    for comp in soup.select("div.component"):
        h2 = comp.find("h2")
        if not h2:
            continue  # page header (logo, term, CRN)
        title, classes, lines = clean(h2.get_text(" ")).lower(), comp.get("class") or [], _lines(comp)
        raw.append(clean(h2.get_text(" ")) + "\n" + "\n".join(lines))
        if "outcome" in title or "objective" in title:
            items = [clean(li.get_text(" ")) for li in comp.find_all("li")]
            doc["outcomes"] += [x for x in items if x] or lines
        elif "material-component" in classes or re.search(r"\b(texts?|textbooks?|books?|materials?)\b", title):
            # not 'resources': 'Student Support & Wellness Resources' is advice, not course materials
            doc["materials"] += _materials(comp)
        elif "instructor-component" in classes or "instructor" in title:
            doc.setdefault("instructor", lines[0] if lines else None)
        elif re.search(r"schedule|calendar|topic|week", title):
            for tr in comp.find_all("tr"):
                cells = [clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
                cells = [c for c in cells if c]
                if len(cells) >= 2:
                    doc["topics"].append({"week": cells[0][:40], "title": cells[1][:300],
                                          "details": " | ".join(cells[2:])[:1000] or None})
        elif "grading" in title or "grade" in title or "evaluation" in title:
            rows = [" ".join(filter(None, (clean(td.get_text(" ")) for td in tr.find_all(["td", "th"]))))
                    for tr in comp.find_all("tr")]
            doc["grading_scale"] = "; ".join(r for r in (rows or lines) if r)[:1000] or None
            weight_lines += rows + lines
        elif re.search(r"assignment|assessment|evaluation", title):
            weight_lines += lines
        elif "description" in title:
            doc["summary"] = re.sub(r"^Credit Hours:\s*[\d.]+\.?\s*", "", " ".join(lines))[:3000] or None
        elif "general course information" in title:
            for i, line in enumerate(lines[:-1]):
                if line.rstrip(":").lower() == "modality":
                    doc["delivery_mode"] = lines[i + 1]
    # Only explicit 'Name NN%' weights that add up to ~100; unweighted assessments are left to the LLM pass,
    # which reads names and point-based weights more reliably than a line heuristic.
    doc["assessments"] = syllabi.weights(weight_lines)
    doc["raw_text"] = "\n\n".join(raw)
    return doc


# --- run ----------------------------------------------------------------------------------------------

def discover(conn, fetcher: Fetcher, institution: Optional[str] = None, progress=lambda m: None) -> dict:
    """Find and store public Simple Syllabus syllabi for the AI courses of the portal schools.
    Returns counts: courses searched, courses with a syllabus, documents saved."""
    counts = {"courses": 0, "found": 0, "documents": 0}
    for inst_name, (slug, marker) in PORTALS.items():
        if institution and institution.lower() not in inst_name.lower():
            continue
        courses = conn.execute("SELECT c.id, c.code, c.title FROM courses c JOIN institutions i "
                               "ON i.id = c.institution_id WHERE i.name = ? AND c.is_ai = 1 ORDER BY c.code",
                               (inst_name,)).fetchall()
        for c in courses:
            counts["courses"] += 1
            try:
                items = pick(search(slug, c["code"]), c["code"], marker)
            except (httpx.HTTPError, BlockedError, ValueError) as e:
                log.warning("Simple Syllabus search failed for %s %s: %s", inst_name, c["code"], e)
                continue
            progress(f"{inst_name} {c['code']}: {len(items)} syllabi")
            saved = 0
            for it in items:
                url = f"https://{slug}.simplesyllabus.com/doc/{it['code']}?mode=view"
                try:
                    doc = parse_page(fetcher.get_rendered(url))
                except Exception as e:  # blocked, or a page layout the parser can't read: keep going
                    log.warning("skipped %s: %s: %s", url, type(e).__name__, e)
                    continue
                year = term_key(it.get("term_name"))[0]
                m = re.search(r"(winter|spring|summer|fall)", it.get("term_name") or "", re.I)
                doc.update(term=m.group(1).title() if m else None, year=year or None,
                           extra={"section": it.get("title")})
                syllabi.save_document(conn, [c["id"]], url, "portal", doc, match_confidence(it, c["title"]),
                                      "portal_code", source_type="html")
                saved += 1
            conn.commit()
            counts["documents"] += saved
            counts["found"] += bool(saved)
    return counts
