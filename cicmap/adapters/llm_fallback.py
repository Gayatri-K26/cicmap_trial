"""LLM fallback for catalog pages no deterministic parser recognizes (e.g. Hallmark, BYU, Miami).

The page is reduced to plain text with link targets kept, and DeepSeek reads the program metadata and
curriculum from it. Guard rails against made-up data:
  * a course is kept only if its code or title actually appears in the page text;
  * a detail link is kept only if it is a real link on the page;
  * course details come from the course's own page, or from the text right after the course on the
    program page (sites like Hallmark list descriptions inline).

Courses listed without a code (BYU links titles to /courses/<id>) get a temporary '~' code; the real
code is read from the course page and the pipeline swaps it in.

Never chosen by detect(): the pipeline uses it only when no other parser matches, and only with the LLM on.
"""
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urldefrag

from bs4 import BeautifulSoup

from ..llm import LLMError
from ..models import CourseData, CurriculumEntry, ProgramData
from ..text import clean, join_spaced_subjects, normalize_code, parse_credits
from .base import CatalogAdapter, register

TEMP_PREFIX = "~"
CHUNK_CHARS = 30000
SNIPPET_CHARS = 2500
COURSEDOG_ID_RE = re.compile(r"^(\d{5}-\d{3}|[A-Za-z0-9]{20})$")

DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "button", "select"]


def page_text(url: str, html: str) -> Tuple[str, List[str]]:
    """Readable text of a page, with links written as 'text <url>' and headings as '## text'."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(DROP_TAGS):
        t.decompose()
    root = soup.find("main")
    if root is None or len(root.get_text(strip=True)) < 1500:
        root = soup.body or soup
    links = []
    for a in root.find_all("a", href=True):
        href, _ = urldefrag(urljoin(url, a["href"]))
        if href.startswith("http") and href != urldefrag(url)[0]:
            links.append(href)
            a.append(f" <{href}>")
    for h in root.find_all(["h1", "h2", "h3", "h4", "h5"]):
        h.insert(0, "## ")
    lines, prev = [], None
    for line in root.get_text("\n").splitlines():
        line = clean(line)
        if line and line != prev:
            lines.append(line)
            prev = line
    return "\n".join(lines), links


def _chunks(text: str, size: int = CHUNK_CHARS) -> List[str]:
    out, cur, n = [], [], 0
    for line in text.splitlines():
        if n + len(line) > size and cur:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def code_pattern(code: str) -> re.Pattern:
    """Loose regex for a normalized code in page text: 'CS 111' matches 'C S 111', 'CS-111', 'CS111'."""
    subj, num = code.split(" ", 1)
    letters = r"[\s\-]?".join(re.escape(ch) for ch in subj)
    return re.compile(rf"\b{letters}[\s *\-]*{re.escape(num)}\b", re.I)


def _course_heading_pos(text: str, entry: CurriculumEntry) -> Optional[int]:
    """Where a course's own listing starts: the mention of its code followed closely by its title, not a
    mention inside another course's prerequisites. Falls back to the first mention."""
    matches = list(code_pattern(entry.code).finditer(text))
    if not matches:
        return None
    title = (entry.title or "").lower()[:40]
    for m in matches:
        if title and title in text[m.end():m.end() + 200].lower():
            return m.start()
    return matches[0].start()


PROGRAM_SYSTEM = (
    "You read university program web pages and extract the degree curriculum. Answer with JSON only. "
    "Copy codes, titles and links exactly as they appear; never invent courses or URLs. Use null when unknown.")

PROGRAM_USER = """Institution: {institution}
Program: {program}
Page URL: {url}
Page text (part {part} of {parts}; links appear as 'text <url>'):
---
{text}
---
Return {{"program": {{"name": str, "degree": short like "BS", "degree_full": str, "college": str,
"department": str, "catalog_year": str, "total_credits": str, "description": short str}},
"curriculum_url": url of a page with the FULL course list, only if this page shows just part of the
curriculum and links to it, else null,
"courses": [{{"code": course code as written or null, "title": str, "credits": str or null,
"requirement_group": heading the course is listed under, "is_alternative": true if it is one choice among
options, "detail_url": the course's own link from the page or null}}]}}
List every course in the program's requirements on this page, including electives."""

COURSE_SYSTEM = ("You extract one course's catalog details from web page text. Answer with JSON only. "
                 "Copy text verbatim; use null when a field is not present. Ignore other courses in the text.")

COURSE_USER = """Course wanted: {code} {title}
Text:
---
{text}
---
Return {{"code": str, "title": str, "credits": str, "description": str, "prereq_text": str,
"coreq_text": str, "restrictions": str, "extra": {{"<other label>": "<value>"}}}}"""


@register
class LLMFallbackAdapter(CatalogAdapter):
    name = "llm"
    version = 1

    def __init__(self, llm=None, institution: str = "", program: str = ""):
        self.llm = llm
        self.institution = institution
        self.program = program
        self.fetcher = None  # set by the pipeline; used to look up Coursedog course ids
        self._text = ""
        self._id_codes: Dict[str, Optional[str]] = {}

    def detect(self, url: str, html: str) -> float:
        return 0.0  # only used explicitly, as the last resort

    # --- program page -------------------------------------------------------------------------------

    def parse_program(self, url: str, html: str) -> ProgramData:
        if self.llm is None or not self.llm.enabled:
            raise LLMError("LLM fallback parser needs the LLM enabled")
        text, links = page_text(url, html)
        self._text = text
        link_set = set(links)
        parts = _chunks(text)
        p = ProgramData(header_text=text[:3000])
        entries: List[CurriculumEntry] = []
        seen = set()
        for i, part in enumerate(parts, start=1):
            data = self.llm.json("llm_parse_program", self.version, PROGRAM_SYSTEM, PROGRAM_USER.format(
                institution=self.institution, program=self.program, url=url, part=i, parts=len(parts), text=part),
                max_tokens=8000)
            meta = data.get("program") or {}
            for f in ("name", "degree", "degree_full", "college", "department", "catalog_year",
                      "total_credits", "description"):
                if not getattr(p, f) and meta.get(f):
                    setattr(p, f, (clean(str(meta[f])) or "")[:2000] or None)
            cu = data.get("curriculum_url")
            if not p.curriculum_url and cu in link_set:
                p.curriculum_url = cu
            for c in data.get("courses") or []:
                e = self._entry(c, part, link_set)
                if e and e.code not in seen:
                    seen.add(e.code)
                    entries.append(e)
        p.entries = entries
        return p

    def _entry(self, c: dict, part: str, link_set) -> Optional[CurriculumEntry]:
        title = clean(c.get("title"))
        detail = c.get("detail_url") if c.get("detail_url") in link_set else None
        code = normalize_code(str(c.get("code") or ""))
        if code and not code_pattern(code).search(part):
            code = None  # not on the page: don't trust it
        title_on_page = bool(title) and title.lower()[:60] in part.lower()
        if not code:
            if not (detail and title_on_page):
                return None
            code = TEMP_PREFIX + detail  # real code comes from the course page
        elif not title_on_page and not code_pattern(code).search(part):
            return None
        return CurriculumEntry(code=code, title=title, credits_text=clean(str(c["credits"])) if c.get("credits") else None,
                               requirement_group=clean(c.get("requirement_group")),
                               is_alternative=bool(c.get("is_alternative")), detail_url=detail)

    # --- course details ----------------------------------------------------------------------------

    def inline_course(self, entry: CurriculumEntry) -> Optional[CourseData]:
        """Details written right after the course on the program page (Hallmark style)."""
        if entry.detail_url or entry.code.startswith(TEMP_PREFIX) or not self._text:
            return None
        start = _course_heading_pos(self._text, entry)
        if start is None:
            return None
        snippet = self._text[start:start + SNIPPET_CHARS]
        return self._extract(entry, snippet, None)

    def parse_course(self, entry: CurriculumEntry, url: str, html: str) -> CourseData:
        html = self._resolve_coursedog_ids(url, html)
        text, _ = page_text(url, html)
        if not entry.code.startswith(TEMP_PREFIX):
            m = code_pattern(entry.code).search(text)
            if m:  # skip site navigation before the course itself
                text = text[max(0, m.start() - 200):]
        return self._extract(entry, text[:12000], url)

    def _resolve_coursedog_ids(self, url: str, html: str) -> str:
        """Coursedog (BYU) renders requisites as internal course ids ('01499-002 or 12181-001') that the
        browser swaps for codes. Look each id up on its own /courses/<id> page (title 'C S 312 Course | ...')."""
        if "req-courses__name" not in html or self.fetcher is None:
            return html
        soup = BeautifulSoup(html, "lxml")
        for btn in soup.find_all("button"):  # requisite labels ('Prerequisite') sit in toggle buttons
            btn.name = "div"
        for name in soup.select(".req-courses__name > span:not(.req-courses__conjunction)"):
            cid = name.get_text(strip=True)
            if COURSEDOG_ID_RE.match(cid):
                code = self._coursedog_code(urljoin(url, f"/courses/{cid}"))
                if code:
                    name.string = code
                else:  # retired course version: drop the option rather than invent a placeholder
                    row = name.find_parent(attrs={"role": "listitem"}) or name.parent
                    row.decompose()
        return str(soup)

    def _coursedog_code(self, course_url: str) -> Optional[str]:
        if course_url not in self._id_codes:
            code = None
            try:
                soup = BeautifulSoup(self.fetcher.get(course_url), "lxml")
                title = soup.title.get_text() if soup.title else ""
                if " Course |" in title:
                    code = normalize_code(title.split(" Course |")[0])
            except Exception:  # missing or retired course version
                pass
            self._id_codes[course_url] = code
        return self._id_codes[course_url]

    def _extract(self, entry: CurriculumEntry, text: str, url: Optional[str]) -> CourseData:
        want = "" if entry.code.startswith(TEMP_PREFIX) else entry.code
        data = self.llm.json("llm_parse_course", self.version, COURSE_SYSTEM,
                             COURSE_USER.format(code=want, title=entry.title or "", text=text), max_tokens=2000)
        code = entry.code
        if code.startswith(TEMP_PREFIX):
            found = normalize_code(str(data.get("code") or ""))
            code = found if found and code_pattern(found).search(text) else code
        credits = clean(str(data["credits"])) if data.get("credits") else entry.credits_text
        lo, hi = parse_credits(credits)
        if lo is not None and lo == hi:
            credits = f"{lo:g}"  # 'Min 3, Fixed/Max 3' -> '3'
        extra: Dict[str, str] = {str(k)[:80]: clean(str(v))[:2000] for k, v in (data.get("extra") or {}).items()
                                 if v and clean(str(v))}
        return CourseData(
            code=code, title=clean(data.get("title")) or entry.title, credits_text=credits,
            credits_min=lo, credits_max=hi, description=clean(data.get("description")),
            prereq_text=join_spaced_subjects(clean(data.get("prereq_text"))),
            coreq_text=join_spaced_subjects(clean(data.get("coreq_text"))),
            restrictions=clean(data.get("restrictions")), extra=extra, source_url=url)
