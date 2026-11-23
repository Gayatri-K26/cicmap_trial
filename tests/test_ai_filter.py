"""AI filter: what happens when a course has no description (no network)."""
from cicmap.ai_filter import TITLE_ONLY_MAX, classify_details
from cicmap.models import CourseData


class FakeLLM:
    enabled = True

    def __init__(self, answer):
        self.answer = answer

    def json(self, purpose, version, system, user, max_tokens=4000):
        return self.answer


class NoLLM:
    enabled = False


COURSES = [CourseData(code="CS 1", title="Machine Learning", description="Covers supervised learning and more."),
           CourseData(code="CS 2", title="Embedded Design: Enabling Robotics", description=None)]


def test_title_only_verdict_is_flagged_and_capped():
    llm = FakeLLM({"courses": [
        {"code": "CS 1", "is_ai": True, "category": "machine_learning", "confidence": 0.97, "reason": "ML."},
        {"code": "CS 2", "is_ai": True, "category": "robotics", "confidence": 0.7, "reason": "Robotics."}]})
    out = classify_details(llm, "BS CS", COURSES)
    assert out["CS 1"]["confidence"] == 0.97 and out["CS 1"]["reason"] == "ML."
    assert out["CS 2"]["is_ai"] and out["CS 2"]["confidence"] == TITLE_ONLY_MAX
    assert out["CS 2"]["reason"].startswith("Judged on title only")


def test_title_only_flag_without_llm():
    out = classify_details(NoLLM(), "BS CS", COURSES)
    assert out["CS 2"]["reason"].startswith("Judged on title only")
    assert out["CS 2"]["confidence"] <= TITLE_ONLY_MAX
