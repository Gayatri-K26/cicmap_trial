"""SmartCatalog catalogs (*.smartcatalogiq.com: Kean, Marymount, Middle Georgia State, Rhode Island College).

Program pages list requirements as tables under sc-RequiredCoursesHeading* headings, with each
course linked via <a class="sc-courselink">. Each course has its own page with .desc, .sc_credits,
.sc_prereqs and .sc_coreqs blocks.
"""
import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import CourseData, CurriculumEntry, ProgramData
from ..text import clean, guess_degree, normalize_code, parse_credits
from .base import CatalogAdapter, register

HEADING_TAGS = ("h2", "h3", "h4", "h5")


@register
class SmartCatalogAdapter(CatalogAdapter):
    name = "smartcatalog"
    version = 2
    domains = ("smartcatalogiq.com",)

    def detect(self, url: str, html: str) -> float:
        low = html.lower()
        score = 0.0
        if "smartcatalogiq" in low:
            score += 0.5
        if "sc-courselink" in low:
            score += 0.4
        if "sc-requiredcoursesheading" in low:
            score += 0.1
        return min(score, 1.0)

    def parse_program(self, url: str, html: str) -> ProgramData:
        soup = BeautifulSoup(html, "lxml")
        main = soup.select_one("#main") or soup
        h1 = main.find("h1")
        name = clean(h1.get_text(" ")) if h1 else None

        crumbs = []
        bc = soup.select_one("#breadcrumbs") or soup.select_one("#breadcrumb")
        if bc:
            crumbs = [c for c in (clean(p) for p in re.split(r"\s*>\s*", bc.get_text(" "))) if c]
        catalog_year = None
        if crumbs:
            ym = re.search(r"(20\d\d)\s*[-–]\s*(20\d\d)", crumbs[0])
            catalog_year = f"{ym.group(1)}-{ym.group(2)}" if ym else None
        college = department = None
        for c in crumbs[1:-1]:
            low = c.lower()
            if "department" in low and not department:
                department = c
            elif ("college" in low or "school" in low) and "programs by" not in low and not college:
                college = c
        degree, degree_full = guess_degree(name, url)

        entries, seen = [], set()
        group_stack = {}
        for el in main.find_all(list(HEADING_TAGS) + ["tr"]):
            if el.name in HEADING_TAGS:
                level = HEADING_TAGS.index(el.name)
                group_stack = {k: v for k, v in group_stack.items() if k < level}
                group_stack[level] = _strip_credits(clean(el.get_text(" ")))
                continue
            link = el.select_one("a.sc-courselink")
            if not link:
                continue
            code = normalize_code(link.get_text(" "))
            if not code or code in seen:
                continue
            seen.add(code)
            title_td = el.select_one("td.sc-coursetitle")
            credit_td = el.select_one("td[class*=sc-credit]")
            group = " > ".join(v for k, v in sorted(group_stack.items()) if v)
            entries.append(CurriculumEntry(
                code=code,
                title=(clean(title_td.get_text(" ")) or "").rstrip(" *") or None if title_td else None,
                credits_text=clean(credit_td.get_text(" ")) if credit_td else None,
                requirement_group=group or None,
                detail_url=urljoin(url, link.get("href")),
            ))

        intro = main.select_one("p.sc-BodyText")
        description = (clean(intro.get_text(" ")) or "")[:2000] or None if intro else None
        header_text = " | ".join(crumbs) + "\n" + (description or "")[:1500]
        return ProgramData(name=name, degree=degree, degree_full=degree_full, college=college,
                           department=department, catalog_year=catalog_year, description=description,
                           entries=entries, header_text=header_text)

    def parse_course(self, entry: CurriculumEntry, url: str, html: str) -> CourseData:
        soup = BeautifulSoup(html, "lxml")
        main = soup.select_one("#main") or soup
        title = entry.title
        h1 = main.find("h1")
        if h1:
            span = h1.find("span")
            full = clean(h1.get_text(" ")) or ""
            code_txt = clean(span.get_text(" ")) if span else ""
            t = clean(full[len(code_txt):]) if code_txt and full.startswith(code_txt) else full
            title = t or title

        def block_text(sel: str) -> Optional[str]:
            el = main.select_one(sel)
            if not el:
                return None
            for h in el.find_all(["h2", "h3"]):
                h.decompose()
            return clean(el.get_text(" "))

        description = block_text(".desc")
        prereq_text = block_text(".sc_prereqs")
        coreq_text = block_text(".sc_coreqs")
        # Some schools (e.g. Marymount) put prerequisites and credits inside the description instead.
        if description and not prereq_text:
            prereq_text = _from_description(description, r"prerequisites?(?:\(s\))?")
        if description and not coreq_text:
            coreq_text = _from_description(description, r"corequisites?(?:\(s\))?")
        credits_text = block_text(".sc_credits") or entry.credits_text
        if not credits_text and description:
            m = re.search(r"\((\d+(?:\s*[-–]\s*\d+)?)\)\s*$", description)
            credits_text = m.group(1) if m else None
        cmin, cmax = parse_credits(credits_text)
        extra = {}
        for sel, label in ((".sc_offered", "Offered"), (".sc_attributes", "Attributes"), (".sc_notes", "Notes")):
            v = block_text(sel)
            if v:
                extra[label] = v
        return CourseData(code=entry.code, title=title, credits_text=credits_text, credits_min=cmin,
                          credits_max=cmax, description=description, prereq_text=prereq_text,
                          coreq_text=coreq_text, extra=extra, source_url=url)


def _from_description(desc: str, label: str) -> Optional[str]:
    """'... Prerequisite: DATA 325 with a grade of C or higher. (3)' -> 'DATA 325 with a grade of C or higher'."""
    m = re.search(label + r"\s*:\s*(.+?)(?:\.\s+(?=[A-Z(])|\.?\s*\(\d+(?:\s*[-–]\s*\d+)?\)\s*$|\.?\s*$)", desc, re.I)
    return clean(m.group(1)) if m else None


def _strip_credits(s: Optional[str]) -> Optional[str]:
    """'General Education 31-32' -> 'General Education'."""
    if not s:
        return s
    return clean(re.sub(r"\s+\d+(\s*[-–]\s*\d+)?\s*(credits?|hours?)?$", "", s, flags=re.I))
