"""Decide which curriculum courses are AI-related.

Stage 1 (titles only, whole curriculum): keyword screen + one batched LLM call -> yes / maybe / no.
Stage 2 (after fetching details for yes + maybe): one batched LLM call with descriptions -> final verdict.
Without the LLM, keywords alone decide (method='keyword').
"""
import json
import re
from typing import Dict, List, Optional

from .llm import LLM, LLMError
from .models import CourseData, CurriculumEntry

STRONG = re.compile(
    r"artificial intelligence|\bai\b|machine learning|deep learning|neural net|reinforcement learning|"
    r"natural language|\bnlp\b|computer vision|generative|large language|\bllms?\b|intelligent (systems|agents)|"
    r"robot|autonomous|pattern recognition|data mining|knowledge representation|cognitive computing|"
    r"speech recognition|image (processing|understanding)|predictive (analytics|model)", re.I)
WEAK = re.compile(
    r"data science|analytics|big data|vision|language|agent|recommend|optimization|statistical learning|"
    r"bayesian|probabilistic|inference|search|algorithm|intelligen|smart|automation|ethic", re.I)

TITLE_ONLY_MAX = 0.6  # highest confidence for a course whose catalog entry has no description

CATEGORIES = ["foundations", "machine_learning", "deep_learning", "nlp", "computer_vision", "robotics",
              "reinforcement_learning", "generative_ai", "data_science_ml", "ai_ethics_policy",
              "ai_applications", "ai_capstone_project", "other_ai"]

DEFINITION = (
    "A course is AI-related if artificial intelligence or machine learning is its primary subject or a major "
    "part of its content: e.g. AI foundations, machine learning, deep learning, neural networks, NLP, computer "
    "vision, robotics/autonomous systems, reinforcement learning, generative AI, knowledge representation and "
    "reasoning, data mining/data science courses centered on ML, AI ethics/policy, applied AI, and AI capstones. "
    "NOT AI-related: general programming, data structures, math/statistics foundations (calculus, linear "
    "algebra, probability), general CS/IT, databases, and general-education courses, even if they are "
    "prerequisites for AI courses.")


def keyword_label(title: str, description: str = "") -> str:
    text = f"{title or ''} {description or ''}"
    if STRONG.search(title or "") or (description and len(STRONG.findall(description)) >= 2):
        return "yes"
    if STRONG.search(text) or WEAK.search(title or ""):
        return "maybe"
    return "no"


def screen_titles(llm: LLM, program_name: str, entries: List[CurriculumEntry]) -> Dict[str, str]:
    """code -> 'yes' | 'maybe' | 'no'. Keyword 'yes' always survives; the LLM can only add candidates."""
    labels = {e.code: keyword_label(e.title or "") for e in entries}
    if not llm.enabled or not entries:
        return labels
    listing = "\n".join(f"{e.code} | {e.title or '?'} | {e.requirement_group or ''}" for e in entries)
    system = ("You classify university courses. " + DEFINITION + " Answer with JSON only.")
    user = (f"Program: {program_name}\nCourses (code | title | requirement group):\n{listing}\n\n"
            'Return {"courses": {"<code>": "yes" | "maybe" | "no"}} for every code. Use "maybe" when the title '
            "alone is not enough to decide (e.g. special topics, capstones, data courses).")
    try:
        data = llm.json("ai_screen_titles", 1, system, user)
        for code, label in (data.get("courses") or {}).items():
            if code in labels and label in ("yes", "maybe") and labels[code] != "yes":
                labels[code] = label
    except LLMError:
        pass
    return labels


def classify_details(llm: LLM, program_name: str, courses: List[CourseData]) -> Dict[str, dict]:
    """code -> {is_ai, category, confidence, reason, method}."""
    out = {}
    for c in courses:
        label = keyword_label(c.title or "", c.description or "")
        out[c.code] = {"is_ai": label == "yes", "category": None, "confidence": 0.9 if label == "yes" else 0.4,
                       "reason": f"keyword screen: {label}", "method": "keyword"}
    if not llm.enabled or not courses:
        return _flag_title_only(courses, out)
    payload = [{"code": c.code, "title": c.title, "description": (c.description or "")[:900]} for c in courses]
    system = ("You classify university courses. " + DEFINITION + " Answer with JSON only.")
    user = (f"Program: {program_name}\nCourses:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            'Return {"courses": [{"code": str, "is_ai": bool, "category": one of '
            f"{CATEGORIES} or null, "
            '"confidence": number 0-1, "reason": short string}]} covering every code.')
    try:
        data = llm.json("ai_classify_details", 1, system, user, max_tokens=6000)
        for item in data.get("courses") or []:
            code = item.get("code")
            if code in out:
                cat = item.get("category")
                out[code] = {"is_ai": bool(item.get("is_ai")),
                             "category": cat if cat in CATEGORIES else None,
                             "confidence": _num(item.get("confidence")),
                             "reason": (item.get("reason") or "")[:300], "method": "llm"}
    except LLMError:
        pass
    return _flag_title_only(courses, out)


def _flag_title_only(courses: List[CourseData], out: Dict[str, dict]) -> Dict[str, dict]:
    """No description: the verdict rests on the title alone, so say so and trust it less."""
    for c in courses:
        if not (c.description or "").strip():
            v = out[c.code]
            v["confidence"] = min(v["confidence"] if v["confidence"] is not None else 1.0, TITLE_ONLY_MAX)
            v["reason"] = "Judged on title only (no description found). " + v["reason"]
    return out


def _num(v) -> Optional[float]:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None
