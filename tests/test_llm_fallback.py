"""LLM fallback parser (Hallmark, BYU, Miami) with a fake LLM, plus the related CourseLeaf/prereq fixes."""
from pathlib import Path

import pytest

from cicmap.adapters import get_adapter
from cicmap.adapters.llm_fallback import TEMP_PREFIX, LLMFallbackAdapter, page_text
from cicmap.models import CurriculumEntry
from cicmap.pipeline import _pick_adapter
from cicmap.prereqs import _clean_groups
from cicmap.text import join_spaced_subjects, normalize_code

FIX = Path(__file__).parent / "fixtures"
BYU = "https://catalog.byu.edu/programs/34716"
BYU_CS474 = "https://catalog.byu.edu/courses/13781-000"
BYU_ML_LINK = "https://catalog.byu.edu/courses/dBwXcrBxxGfbtBqMvDyJ"
HALLMARK = "https://hallmark.edu/schools/bachelor-of-science-artificial-intelligence/"
MIAMI = "https://programs.miamioh.edu/programs/artificial-intelligence-b-s/"
MIAMI_FULL = "https://bulletin.miamioh.edu/engineering-computing/artificial-intelligence-bsai/"


def read(name):
    return (FIX / name).read_text()


class FakeLLM:
    """Returns canned answers by purpose and records the prompts it was given."""
    enabled = True

    def __init__(self, answers):
        self.answers = answers
        self.prompts = []

    def json(self, purpose, version, system, user, max_tokens=4000):
        self.prompts.append((purpose, user))
        return self.answers[purpose]


def adapter(answers):
    a = LLMFallbackAdapter(FakeLLM(answers), "U", "BS AI")
    return a


# --- text helpers -------------------------------------------------------------------------------

def test_spaced_subjects():
    assert normalize_code("C S 111") == "CS 111"
    assert normalize_code("EC EN 220") == "ECEN 220"
    assert join_spaced_subjects("C S 312 or MATH 320") == "CS 312 or MATH 320"
    assert join_spaced_subjects("MATH 112 OR CS 235") == "MATH 112 OR CS 235"


def test_page_text_keeps_links():
    text, links = page_text(BYU, read("llm_byu_program.html"))
    assert f"<{BYU_ML_LINK}>" in text
    assert "- Intro to Machine Learning 3.0" in text
    assert BYU_ML_LINK in links


# --- program page -----------------------------------------------------------------------------------

def test_program_drops_courses_not_on_page():
    a = adapter({"llm_parse_program": {"program": {"name": "BS in AI", "degree": "BS"}, "curriculum_url": None,
                                       "courses": [
        {"code": "ITAI-3310", "title": "Machine Learning I", "requirement_group": "Core Courses"},
        {"code": "ZZZ 9999", "title": "Quantum Basket Weaving"},              # invented
        {"code": None, "title": "Machine Learning I", "detail_url": "https://evil.example/x"},  # link not on page
    ]}})
    p = a.parse_program(HALLMARK, read("llm_hallmark_program.html"))
    assert [e.code for e in p.entries] == ["ITAI 3310"]
    assert p.entries[0].requirement_group == "Core Courses"
    assert p.degree == "BS"


def test_program_without_codes_gets_temp_code():
    a = adapter({"llm_parse_program": {"program": {}, "courses": [
        {"code": None, "title": "Intro to Machine Learning", "credits": "3.0", "detail_url": BYU_ML_LINK}]}})
    p = a.parse_program(BYU, read("llm_byu_program.html"))
    assert p.entries[0].code == TEMP_PREFIX + BYU_ML_LINK
    assert p.entries[0].detail_url == BYU_ML_LINK


def test_curriculum_url_must_be_a_page_link():
    answers = {"llm_parse_program": {"program": {}, "curriculum_url": MIAMI_FULL, "courses": []}}
    assert adapter(answers).parse_program(MIAMI, read("llm_miami_program.html")).curriculum_url == MIAMI_FULL
    answers["llm_parse_program"]["curriculum_url"] = "https://made-up.example/full"
    assert adapter(answers).parse_program(MIAMI, read("llm_miami_program.html")).curriculum_url is None


# --- course details ---------------------------------------------------------------------------------

def test_inline_course_snippet_starts_at_its_own_listing():
    a = adapter({"llm_parse_program": {"program": {}, "courses": []},
                 "llm_parse_course": {"code": "ITAI-3310", "title": "Machine Learning I", "credits": None,
                                      "description": "d", "prereq_text": None}})
    a.parse_program(HALLMARK, read("llm_hallmark_program.html"))
    c = a.inline_course(CurriculumEntry(code="ITAI 3310", title="Machine Learning I"))
    snippet = a.llm.prompts[-1][1].split("---\n", 1)[1]
    assert snippet.startswith("ITAI-3310\nMachine Learning I")  # not the earlier prerequisite mention
    assert c.code == "ITAI 3310"


def test_inline_course_snippet_stops_at_next_course():
    a = adapter({"llm_parse_program": {"program": {}, "courses": [
                    {"code": "ITAI-4370", "title": "AI Practicum (Industry Project)"},
                    {"code": "ITAI-4475", "title": "AI Capstone Project"}]},
                 "llm_parse_course": {"code": "ITAI-4370", "title": "AI Practicum", "prereq_text": "ITAI-4440"}})
    a.parse_program(HALLMARK, read("llm_hallmark_program.html"))
    a.inline_course(CurriculumEntry(code="ITAI 4370", title="AI Practicum (Industry Project)"))
    snippet = a.llm.prompts[-1][1].split("---\n", 1)[1].rsplit("\n---", 1)[0]
    assert "ITAI-4440" in snippet
    assert "ITAI-4475" not in snippet  # the next course's code used to be read as a prerequisite


def test_coursedog_ids_resolved_and_retired_dropped():
    class FakeFetcher:
        def get(self, url):
            pages = {"01499-002": "C S 312", "12181-001": "MATH 320"}
            cid = url.rsplit("/", 1)[1]
            if cid not in pages:
                raise RuntimeError("404")
            return f"<html><head><title>{pages[cid]} Course | BYU Catalog</title></head></html>"

    a = adapter({"llm_parse_course": {"code": "C S 474", "title": "Introduction to Deep Learning",
                                      "credits": "Min 3, Fixed/Max 3", "prereq_text": "C S 312 or MATH 320"}})
    a.fetcher = FakeFetcher()
    c = a.parse_course(CurriculumEntry(code=TEMP_PREFIX + BYU_CS474, title="Deep Learning"), BYU_CS474,
                       read("llm_byu_cs474.html"))
    sent = a.llm.prompts[-1][1]
    assert "CS 312" in sent and "MATH 320" in sent and "Prerequisite" in sent
    assert "01499-002" not in sent and "10322-004" not in sent
    assert c.code == "CS 474"           # learned from the course page
    assert c.credits_text == "3" and c.credits_min == 3
    assert c.prereq_text == "CS 312 or MATH 320"


# --- adapter choice ---------------------------------------------------------------------------------

def test_fallback_only_when_llm_enabled():
    src = {"adapter_override": None, "institution": "U", "program": "BS AI"}
    a, _ = _pick_adapter(src, HALLMARK, read("llm_hallmark_program.html"), FakeLLM({}), fetcher=None)
    assert isinstance(a, LLMFallbackAdapter)
    off = FakeLLM({})
    off.enabled = False
    assert _pick_adapter(src, HALLMARK, read("llm_hallmark_program.html"), off, fetcher=None)[0] is None


# --- related fixes --------------------------------------------------------------------------------

def test_courseleaf_miami_credits_and_inline_prereq():
    c = get_adapter("courseleaf").parse_course(CurriculumEntry(code="CSE 268"), "u", read("courseleaf_miami_cse268.xml"))
    assert c.title == "Introduction to Knowledge Representation"
    assert c.credits_min == 3
    assert c.prereq_text == "CSE 232 OR CSE 271"
    assert "Prerequisite" not in c.description


def test_prereq_groups_accept_bare_codes():
    assert _clean_groups([["CSE 232", "CSE 271"]]) == [[
        {"code": "CSE 232", "min_grade": None, "concurrent_ok": False},
        {"code": "CSE 271", "min_grade": None, "concurrent_ok": False}]]
