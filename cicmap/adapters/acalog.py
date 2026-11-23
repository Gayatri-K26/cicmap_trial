"""Acalog catalogs (preview_program.php: Capitol Tech, Dakota State, Ferris, Harding, Purdue).

Program pages are usually behind an AWS WAF bot check (handled by the browser tier in fetch.py).
Requirements sit in <div class="acalog-core"> sections under h2-h5 headings; each course is an
<li class="acalog-course"> whose link calls showCourse('<catoid>', '<coid>'). Course pages
(preview_course_nopop.php) have a title h1 followed by free-form text whose labels differ by school,
so fields are read from labeled lines rather than fixed positions.
"""
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, Tag

from ..models import CourseData, CurriculumEntry, ProgramData
from ..text import clean, guess_degree, normalize_code, parse_credits
from .base import CatalogAdapter, register

SHOWCOURSE_RE = re.compile(r"showCourse\(\s*'(\d+)'\s*,\s*'(\d+)'")
COID_RE = re.compile(r"catoid=(\d+)&(?:amp;)?coid=(\d+)")
HEADINGS = ("h2", "h3", "h4", "h5")

# label (lowercase, normalized) -> CourseData field; anything else labeled goes to `extra`
FIELD_LABELS = {
    "prerequisite": "prereq_text", "prerequisites": "prereq_text", "prerequisite(s)": "prereq_text",
    "pre-requisite(s)": "prereq_text", "corequisite": "coreq_text", "corequisites": "coreq_text",
    "corequisite(s)": "coreq_text", "co-requisite(s)": "coreq_text", "pre- or corequisite(s)": "prereq_text",
    "prerequisite or corequisite": "prereq_text", "restriction": "restrictions", "restrictions": "restrictions",
    "restriction(s)": "restrictions",
}
CREDIT_LABELS = ("credit hours", "credit hour(s)", "credits", "credit(s)", "semester credit hour(s)",
                 "semester credit hours", "units")
MULTILINE_LABELS = ("learning outcomes", "course outcomes", "course objectives", "student learning outcomes",
                    "topics", "course content")
# Bold labels written without a colon ("<strong>Prerequisite</strong> ISIN 306") get one added.
BARE_LABEL_RE = re.compile(r"^(pre-?requisites?(\(s\))?|co-?requisites?(\(s\))?|restrictions?(\(s\))?|"
                           r"learning outcomes|course outcomes|course objectives|student learning outcomes|"
                           r"term offered|typically offered|notes?|course attributes)$", re.I)
HR = "\x00HR"
LABEL_LINE = re.compile(r"^\s*([A-Z][A-Za-z0-9 /()&,'.-]{1,45}?)\s*:\s*(.*)$")
SMALL_WORDS = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"}


def looks_like_label(text: str) -> bool:
    """'Term Offered', 'Class Restriction' yes; 'Concepts in Artificial intelligence' (a sentence) no."""
    words = re.findall(r"[A-Za-z]+", text)
    return len(words) <= 4 and all(w[0].isupper() or w.lower() in SMALL_WORDS for w in words)


CREDITS_RE = re.compile(
    r"(\d+(?:\.\d+)?(?:\s*(?:-|–|to|or)\s*\d+(?:\.\d+)?)?)\s*(?:semester\s+)?(?:credit|cr\.?\b|units?\b)", re.I)


@register
class AcalogAdapter(CatalogAdapter):
    name = "acalog"
    version = 3

    def detect(self, url: str, html: str) -> float:
        low = html.lower()
        score = 0.0
        if "preview_program.php" in url:
            score += 0.4
        if "acalog-course" in low or "acalog-core" in low:
            score += 0.5
        if "acalog" in low:
            score += 0.1
        return min(score, 1.0)

    def parse_program(self, url: str, html: str) -> ProgramData:
        soup = BeautifulSoup(html, "lxml")
        h1 = soup.select_one("h1#acalog-page-title, h1#acalog-content") or soup.find("h1")
        name = clean(h1.get_text(" ")) if h1 else None

        catalog_year = None
        opt = soup.select_one("select[name=catalog] option[selected]") or soup.find("option", selected=True)
        if opt:
            ym = re.search(r"(20\d\d)\s*[-–]\s*(20\d\d)", opt.get_text(" "))
            catalog_year = f"{ym.group(1)}-{ym.group(2)}" if ym else None
        degree, degree_full = guess_degree(name, url)

        entries: List[CurriculumEntry] = []
        seen = set()
        host = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        cores = soup.select("div.acalog-core")
        heading_stack: Dict[int, str] = {}
        for core in cores:
            for el in core.find_all(list(HEADINGS) + ["li", "a"]):
                if el.name in HEADINGS:
                    level = HEADINGS.index(el.name)
                    heading_stack = {k: v for k, v in heading_stack.items() if k < level}
                    heading_stack[level] = _strip_credits(clean(el.get_text(" ")))
                    continue
                group = " > ".join(v for _, v in sorted(heading_stack.items()) if v) or None
                if el.name == "li" and "acalog-course" in (el.get("class") or []):
                    link = el.find("a")
                    if not link:
                        continue
                    ids = SHOWCOURSE_RE.search(link.get("onclick") or "") or COID_RE.search(link.get("href") or "")
                    label = (link.get("aria-label") or link.get_text(" ")).replace("View course details for", "")
                    code, title = _split_code_title(label)
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    rest = clean(el.get_text(" ").replace(link.get_text(" "), "", 1)) or ""
                    cm = CREDITS_RE.search(rest) or re.search(r"credit hours?\s*:?\s*(\d+(?:\.\d+)?)", rest, re.I)
                    entries.append(CurriculumEntry(
                        code=code, title=title, credits_text=cm.group(1) if cm else None, requirement_group=group,
                        is_alternative=bool(re.match(r"^\s*or\b", el.get_text(" "), re.I)),
                        detail_url=_detail_url(host, ids) if ids else None))
                elif el.name == "a" and not el.find_parent("li", class_="acalog-course"):
                    # Courses mentioned inline in requirement text ("Choose one of CS 101 or ...").
                    ids = COID_RE.search((el.get("href") or "") + " " + (el.get("rel") and " ".join(el.get("rel")) or ""))
                    label = (el.get("aria-label") or "").replace("View course details for", "")
                    code, title = _split_code_title(label or el.get_text(" "))
                    if not ids or not code or code in seen:
                        continue
                    seen.add(code)
                    entries.append(CurriculumEntry(code=code, title=title, requirement_group=group,
                                                   is_alternative=True, detail_url=_detail_url(host, ids)))

        header_text = _header_text(soup, h1, cores[0] if cores else None)
        return ProgramData(name=name, degree=degree, degree_full=degree_full, catalog_year=catalog_year,
                           description=header_text[:2000] or None, entries=entries, header_text=header_text)

    def parse_course(self, entry: CurriculumEntry, url: str, html: str) -> CourseData:
        soup = BeautifulSoup(html, "lxml")
        h1 = soup.select_one("#course_preview_title") or soup.find("h1")
        title = entry.title
        if h1:
            _, t = _split_code_title(h1.get_text(" "))
            title = t or title
        lines = _lines_after(h1) if h1 else []

        fields: Dict[str, str] = {}
        desc_parts: List[str] = []
        credits_text: Optional[str] = None
        multi: Optional[str] = None      # label whose value continues over following lines
        for line in lines:
            if line == HR:
                multi = None
                continue
            m = LABEL_LINE.match(line)
            label = m.group(1).strip().lower() if m else None
            if m and (label in FIELD_LABELS or label in CREDIT_LABELS or label in MULTILINE_LABELS
                      or looks_like_label(m.group(1))):
                value = m.group(2).strip()
                multi = None
                if label in CREDIT_LABELS:
                    # Purdue: "Credit Hours: 3.00. This course provides ..." -> credits + description
                    cm = re.match(r"(\d+(?:\.\d+)?(?:\s*(?:-|–|to|or)\s*\d+(?:\.\d+)?)?)\.?\s*(.*)", value)
                    if cm:
                        credits_text = credits_text or cm.group(1)
                        if len(cm.group(2)) > 40:
                            desc_parts.append(cm.group(2))
                        continue
                if label in MULTILINE_LABELS:
                    multi = m.group(1).strip()
                fields[m.group(1).strip()] = value
                continue
            if multi:
                fields[multi] = (fields[multi] + "\n" + line).strip()
                continue
            cm = re.fullmatch(r"(\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?)\s*(?:semester\s+)?credits?(?: hours?)?(?:\(s\))?",
                              line, re.I)
            if cm:
                credits_text = credits_text or cm.group(1)
                continue
            # Capitol: "Class Hour(s) 3", "3 Semester Credit Hour(s)" without colons
            hm = re.fullmatch(r"(Class|Laboratory|Lab|Lecture|Clinical)\s+Hour\(?s?\)?\s+(\d+(?:\.\d+)?)", line, re.I)
            if hm:
                fields[hm.group(1).title() + " Hours"] = hm.group(2)
                continue
            inline = re.search(r"\bPrerequisites?(?:\(s\))?\s*:\s*(.+)$", line, re.I)
            if inline:  # DSU: prerequisites at the end of the description paragraph
                fields.setdefault("Prerequisite(s)", inline.group(1).strip())
                line = line[:inline.start()].strip()
            if line:
                desc_parts.append(line)

        out = {"prereq_text": None, "coreq_text": None, "restrictions": None}
        extra: Dict[str, str] = {}
        for label, value in fields.items():
            key = FIELD_LABELS.get(label.lower())
            value = clean(value)
            if not value:
                continue
            if key:
                out[key] = out[key] or value
            elif label.lower() in CREDIT_LABELS:
                credits_text = credits_text or value
            else:
                extra[label] = value[:2000]
        description = clean(" ".join(p for p in desc_parts if len(p) > 25)) or None
        cmin, cmax = parse_credits(credits_text)
        return CourseData(code=entry.code, title=title, credits_text=credits_text, credits_min=cmin,
                          credits_max=cmax, description=description, extra=extra, source_url=url, **out)


def _detail_url(host: str, ids) -> str:
    return f"{host}/preview_course_nopop.php?catoid={ids.group(1)}&coid={ids.group(2)}"


def _split_code_title(label: str) -> Tuple[Optional[str], Optional[str]]:
    label = clean((label or "").replace(" ", " ")) or ""
    code = normalize_code(label)
    if not code:
        return None, None
    parts = re.split(r"\s+[-–]\s+", label, maxsplit=1)
    title = clean(parts[1]) if len(parts) == 2 else None
    return code, title


def _strip_credits(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    # "Required Courses (48 Credits)", "University Requirements - 39 to 41 Credits Required"
    s = re.sub(r"\s*[-–(]?\s*\d+(\.\d+)?(\s*(?:-|–|to)\s*\d+(\.\d+)?)?\s*(semester\s+)?(credits?|credit hours?|hours?)"
               r"(\s+required)?(\s*\(or their equivalent\))?\)?\s*$", "", s, flags=re.I)
    return clean(s.rstrip(" :-–")) or s


def _lines_after(h1: Tag) -> List[str]:
    """Text after the course title, one entry per visual line (HR marks <hr>), stopping at the page footer."""
    container = h1.parent
    parts: List[str] = []
    buf: List[str] = []
    started = False

    def flush():
        line = clean("".join(buf))
        if line:
            parts.append(line)
        buf.clear()

    for node in container.descendants:
        if node is h1:
            started = True
            continue
        if not started or (not isinstance(node, Tag) and node.find_parent("h1") is h1):
            continue
        if isinstance(node, Tag):
            if node.name in ("br", "hr", "p", "div", "li"):
                flush()
            if node.name == "hr":
                parts.append(HR)
            if node.name == "div" and "float: right" in (node.get("style") or ""):
                break  # "Back to Top | Print" footer
            continue
        if node.find_parent(["script", "style"]):
            continue
        text = str(node)
        if node.parent.name in ("strong", "b") and BARE_LABEL_RE.match(text.strip()):
            text = text.strip() + ": "
        buf.append(text)
    flush()
    return [p for p in parts if "back to top" not in p.lower()]


def _header_text(soup: BeautifulSoup, h1: Optional[Tag], first_core: Optional[Tag]) -> str:
    if not h1:
        return ""
    chunks: List[str] = []
    for node in h1.next_elements:
        if first_core is not None and node is first_core:
            break
        if isinstance(node, Tag) or node.find_parent(["script", "style", "noscript"]):
            continue
        t = clean(str(node))
        if t:
            chunks.append(t)
        if sum(len(c) for c in chunks) > 3000:
            break
    return clean(" ".join(chunks)) or ""
