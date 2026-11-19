"""Small text helpers: course-code normalization, credits parsing, whitespace cleanup."""
import re
from typing import Optional, Tuple

# Subject (2-5 letters, optional '&') + number (3-5 digits, optional letter suffix; Purdue uses 5).
# Handles 'CS 380', 'CS&#160;380', 'CPS*2800', 'CPS-2800', 'cs380'.
CODE_RE = re.compile(r"\b([A-Z][A-Z&]{1,4})[\s\u00a0*\-]*(\d{3,5}[A-Z]?)\b")


def clean(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", s.replace("\u00a0", " ")).strip()
    s = re.sub(r"\s+([,.;:)])", r"\1", s)  # "Intelligence , BS" -> "Intelligence, BS"
    return s or None


# BYU-style subjects written with spaces: 'C S 111', 'EC EN 220', 'A HTG 100'.
_SPACED_SUBJECT_RE = re.compile(r"\b((?:[A-Z]{1,2} ){1,3}[A-Z]{1,5}) (?=\d{3,5}[A-Z]?\b)")
_NOT_SUBJECT = {"OR", "AND", "IN", "OF", "TO", "NOR", "ON", "AT", "BY"}


def join_spaced_subjects(text: Optional[str]) -> Optional[str]:
    """'C S 111 or EC EN 220' -> 'CS 111 or ECEN 220'. Leaves 'OR CS 235' alone."""
    if not text:
        return text

    def fix(m):
        words = m.group(1).split(" ")
        if any(w in _NOT_SUBJECT for w in words):
            return m.group(0)
        return "".join(words) + " "
    return _SPACED_SUBJECT_RE.sub(fix, text)


def normalize_code(raw: str) -> Optional[str]:
    raw = join_spaced_subjects((raw or "").replace("\u00a0", " ").strip().upper())
    m = CODE_RE.search(raw)
    return f"{m.group(1)} {m.group(2)}" if m else None


def split_code(code: str) -> Tuple[Optional[str], Optional[str]]:
    parts = code.split(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (None, None)


def find_codes(text: str):
    """All normalized course codes in a piece of text, in order, without duplicates."""
    seen, out = set(), []
    for m in CODE_RE.finditer((text or "").replace("\u00a0", " ")):
        code = f"{m.group(1)} {m.group(2)}"
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def parse_credits(text: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    """'3' -> (3,3); '3.0 Credits' -> (3,3); '1-4' or '1 to 4' -> (1,4)."""
    if not text:
        return None, None
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if not nums:
        return None, None
    if re.search(r"\d\s*(-|–|to)\s*\d", text):
        return min(nums[:2]), max(nums[:2])
    return nums[0], nums[0]


DEGREE_PATTERNS = [
    (r"bachelor of science", "BS", "Bachelor of Science"),
    (r"bachelor of arts", "BA", "Bachelor of Arts"),
    (r"master of science", "MS", "Master of Science"),
    (r"master of arts", "MA", "Master of Arts"),
    (r"\bb\.?\s?s\.?\b", "BS", "Bachelor of Science"),
    (r"\bb\.?\s?a\.?\b", "BA", "Bachelor of Arts"),
    (r"\bm\.?\s?s\.?\b", "MS", "Master of Science"),
    (r"\bph\.?\s?d\.?\b", "PhD", "Doctor of Philosophy"),
]


def guess_degree(*texts: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    for text in texts:
        low = (text or "").lower()
        for pat, short, full in DEGREE_PATTERNS:
            if re.search(pat, low):
                return short, full
    return None, None
