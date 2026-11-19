"""CourseLeaf catalogs (e.g. Drexel, Illinois Tech).

Program pages list requirements in <table class="sc_courselist">. Course details come from the
CourseLeaf course endpoint: /ribbit/index.cgi?page=getcourse.rjs&code=CS%20380 (XML wrapping a
<div class="courseblock">).
"""
import re
from typing import Optional
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup, Tag

from ..models import CourseData, CurriculumEntry, ProgramData
from ..text import clean, guess_degree, normalize_code, parse_credits
from .base import CatalogAdapter, register

LABEL_MAP = {
    "prerequisite": "prereq_text", "prerequisites": "prereq_text", "prerequisite(s)": "prereq_text",
    "corequisite": "coreq_text", "corequisites": "coreq_text", "corequisite(s)": "coreq_text",
    "restrictions": "restrictions", "restriction": "restrictions",
}


@register
class CourseLeafAdapter(CatalogAdapter):
    name = "courseleaf"
    version = 2

    def detect(self, url: str, html: str) -> float:
        low = html.lower()
        score = 0.0
        if 'class="sc_courselist' in low:
            score += 0.6
        if "showcourse(this" in low or "/ribbit/" in low:
            score += 0.3
        if "courseleaf" in low:
            score += 0.1
        return min(score, 1.0)

    def parse_program(self, url: str, html: str) -> ProgramData:
        soup = BeautifulSoup(html, "lxml")
        h1 = soup.select_one("h1.page-title") or soup.find("h1")
        name = clean(h1.get_text(" ")) if h1 else None

        crumbs = _breadcrumbs(soup)
        # Breadcrumbs look like: Home > Undergraduate > [Colleges] > College > [Department] > Program
        middle = [c for c in crumbs[1:-1] if c.lower() not in ("undergraduate", "graduate", "colleges", "programs")]
        college = middle[0] if middle else None
        department = middle[1] if len(middle) > 1 else None

        last_crumb = crumbs[-1] if crumbs else None
        degree, degree_full = guess_degree(last_crumb, name, url)

        entries = []
        seen = set()
        for table in soup.select("table.sc_courselist"):
            group = None
            for tr in table.select("tr"):
                classes = tr.get("class") or []
                header = tr.select_one("span.areaheader, span.areasubheader")
                if "areaheader" in classes or "areasubheader" in classes or header:
                    if header:
                        group = clean(header.get_text(" "))
                    continue
                links = tr.select("a.bubblelink.code, a.code")
                if not links:
                    continue
                tds = tr.find_all("td")
                title_cell = tds[1] if len(tds) > 1 else None
                title = _first_line(title_cell) if title_cell else None
                hours_td = tr.select_one("td.hourscol")
                credits = clean(hours_td.get_text(" ")) if hours_td else None
                is_alt = "orclass" in classes
                for i, a in enumerate(links):
                    code = normalize_code(a.get("title") or a.get_text(" "))
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    entries.append(CurriculumEntry(
                        code=code,
                        title=title if i == 0 else None,
                        credits_text=credits if i == 0 else None,
                        requirement_group=group,
                        is_alternative=is_alt,
                    ))

        intro = soup.select_one("#textcontainer")
        description = (clean(intro.get_text(" ")) or "")[:2000] or None if intro else None
        header_text = " | ".join(crumbs) + "\n" + (description or "")[:1500]
        return ProgramData(name=name, degree=degree, degree_full=degree_full, college=college,
                           department=department, description=description, entries=entries,
                           header_text=header_text)

    def course_detail_url(self, program_url: str, entry: CurriculumEntry) -> Optional[str]:
        p = urlparse(program_url)
        return f"{p.scheme}://{p.netloc}/ribbit/index.cgi?page=getcourse.rjs&code={quote(entry.code)}"

    def parse_course(self, entry: CurriculumEntry, url: str, html: str) -> CourseData:
        # The endpoint returns XML with the HTML inside a CDATA block.
        m = re.search(r"<!\[CDATA\[(.*?)\]\]>", html, re.S)
        soup = BeautifulSoup(m.group(1) if m else html, "lxml")
        block = soup.select_one("div.courseblock") or soup

        title, credits_text = entry.title, entry.credits_text
        title_el = block.select_one(".courseblocktitle")
        if title_el:
            t_el = title_el.select_one(".coursetitle")
            if t_el:  # IIT style: separate code / title divs
                title = clean(t_el.get_text(" "))
            else:     # Drexel style: "CS 380  Artificial Intelligence 3.0 Credits"
                spans = title_el.select("span.cdspacing")
                if len(spans) >= 2:
                    title = clean(spans[1].get_text(" "))
                txt = clean(title_el.get_text(" ")) or ""
                cm = (re.search(r"(\d+(?:\.\d+)?(?:\s*(?:-|to)\s*\d+(?:\.\d+)?)?)\s*Credits?", txt, re.I)
                      # Miami style: "CSE 268. Introduction to Knowledge Representation. (3)"
                      or re.search(r"\((\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?)\)\s*$", txt))
                if cm:
                    credits_text = cm.group(1)
                if len(spans) < 2:
                    tm = re.match(r"[A-Z&]+\s*\d+[A-Z]?\.\s+(.+?)\.?\s*(\([^)]*\))?\s*$", txt)
                    if tm:
                        title = tm.group(1).rstrip(".")

        desc_el = block.select_one(".courseblockdesc")
        description = None
        if desc_el:
            description = clean(desc_el.get_text(" "))

        fields = _labeled_fields(block)
        out = {"prereq_text": None, "coreq_text": None, "restrictions": None}
        extra = {}
        for label, value in fields.items():
            key = LABEL_MAP.get(label.lower())
            if key:
                out[key] = value
            elif label.lower() in ("credits", "credit hours"):
                credits_text = value
            else:
                extra[label] = value
        if description and not out["prereq_text"] and not out["coreq_text"]:
            # Miami style: requisites written inside the description ("... Prerequisite: CSE 232 OR CSE 271.")
            description, inline = _split_requisites(description)
            out.update({k: v for k, v in inline.items() if v})
        cmin, cmax = parse_credits(credits_text)
        return CourseData(code=entry.code, title=title, credits_text=credits_text, credits_min=cmin,
                          credits_max=cmax, description=description, extra=extra, source_url=url, **out)


REQ_IN_DESC_RE = re.compile(r"\s*\b(Prerequisites?|Pre-requisites?|Co-?requisites?)(?:\s*\(s\))?\s*:\s*", re.I)


def _split_requisites(description: str):
    """Pull 'Prerequisite: ...' / 'Co-requisite: ...' sentences off the end of a description."""
    parts = REQ_IN_DESC_RE.split(description)
    found = {"prereq_text": None, "coreq_text": None}
    if len(parts) < 3:
        return description, found
    for label, value in zip(parts[1::2], parts[2::2]):
        key = "coreq_text" if label.lower().startswith("co") else "prereq_text"
        found[key] = clean(value.rstrip(". ")) or None
    return clean(parts[0]) or None, found


def _breadcrumbs(soup: BeautifulSoup):
    el = soup.select_one("#breadcrumb") or soup.select_one(".breadcrumbs, #breadcrumbs")
    if not el:
        return []
    items = el.select("li")
    if items:
        parts = [clean(li.get_text(" ")) for li in items]
    else:
        parts = [clean(p) for p in re.split(r"\s*>\s*", el.get_text(" "))]
    return [p.strip(" /") for p in parts if p and p.strip(" /")]


def _first_line(cell: Tag) -> Optional[str]:
    parts = []
    for child in cell.children:
        if isinstance(child, Tag) and child.name == "br":
            break
        parts.append(child.get_text(" ") if isinstance(child, Tag) else str(child))
    return clean("".join(parts))


def _labeled_fields(block: Tag) -> dict:
    """Collect '<b>Label:</b> value' / '<strong>Label:</strong> value' pairs."""
    fields = {}
    for lab in block.find_all(["b", "strong"]):
        label = clean(lab.get_text(" ")) or ""
        if not label.endswith(":"):
            continue
        label = label[:-1].strip()
        if lab.find_parent(class_="courseblocktitle"):
            continue
        parts = []
        for sib in lab.next_siblings:
            if isinstance(sib, Tag) and (sib.name in ("b", "strong", "br") or sib.find(["b", "strong"])):
                break
            parts.append(sib.get_text(" ") if isinstance(sib, Tag) else str(sib))
        value = clean("".join(parts))
        if value:
            fields[label] = value
    return fields
