from pathlib import Path

import pytest

from cicmap import db
from cicmap.adapters import get_adapter, select_adapter
from cicmap.fetch import BlockedError, Fetcher, looks_blocked
from cicmap.models import CurriculumEntry

FIX = Path(__file__).parent / "fixtures"
PURDUE = "https://catalog.purdue.edu/preview_program.php?catoid=19&poid=35981"
HARDING = "https://catalog.harding.edu/preview_program.php?catoid=4&poid=2261"

AWS_WAF_CHALLENGE = """<!DOCTYPE html><html><head><script>window.awsWafCookieDomainList = [];
window.gokuProps = {"key":"x"};</script><script src="https://x.token.awswaf.com/challenge.js"></script></head>
<body><div id="challenge-container"></div><script>AwsWafIntegration.getToken()</script></body></html>"""


def read(name):
    return (FIX / name).read_text()


def course(code, fixture):
    return get_adapter("acalog").parse_course(CurriculumEntry(code=code), "u", read(fixture))


# --- block detection ------------------------------------------------------------

def test_waf_challenge_is_blocked():
    assert looks_blocked(AWS_WAF_CHALLENGE)
    assert looks_blocked("")


@pytest.mark.parametrize("fixture", ["acalog_purdue_program.html", "acalog_harding_program.html",
                                     "courseleaf_drexel_program.html", "smartcatalog_kean_program.html"])
def test_real_pages_not_blocked(fixture):
    assert not looks_blocked(read(fixture))


def test_blocked_page_without_browser_raises(monkeypatch):
    conn = db.connect(Path(":memory:"))
    f = Fetcher(conn, use_browser=False)
    monkeypatch.setattr(f, "_http", lambda url: (AWS_WAF_CHALLENGE, 202))
    with pytest.raises(BlockedError):
        f.get("https://catalog.example.edu/preview_program.php?poid=1")


def test_browser_fallback_and_cookie_reuse(monkeypatch):
    conn = db.connect(Path(":memory:"))

    class FakeBrowser:
        def get(self, url):
            return read("acalog_purdue_program.html"), 200, {"catalog.purdue.edu": {"aws-waf-token": "t"}}

        def close(self):
            pass

    f = Fetcher(conn, browser=FakeBrowser())
    monkeypatch.setattr(f, "_http", lambda url: (AWS_WAF_CHALLENGE, 202))
    html = f.get(PURDUE)
    assert "acalog-course" in html
    assert f.browser_fetches == 1
    assert f._client.cookies.get("aws-waf-token") == "t"
    # Served from cache on the second call: no new fetch.
    f.get(PURDUE)
    assert f.browser_fetches == 1


# --- Acalog parser --------------------------------------------------------------------

def test_acalog_program_purdue_five_digit_codes():
    html = read("acalog_purdue_program.html")
    a, _ = select_adapter(PURDUE, html)
    assert a.name == "acalog"
    p = a.parse_program(PURDUE, html)
    codes = {e.code: e for e in p.entries}
    assert p.name == "Artificial Intelligence, BS"
    assert p.catalog_year == "2026-2027"
    assert "CS 17600" in codes
    assert codes["CS 17600"].requirement_group == "Departmental/Program Major Courses > Required Major Courses"
    assert codes["CS 17600"].detail_url.endswith("preview_course_nopop.php?catoid=19&coid=231175")


def test_acalog_program_harding_title():
    p = get_adapter("acalog").parse_program(HARDING, read("acalog_harding_program.html"))
    assert p.name == "Artificial Intelligence Major (AI), BS"
    assert len(p.entries) > 10


def test_acalog_course_purdue_outcomes_separate():
    c = course("CS 37300", "acalog_purdue_cs37300.html")
    assert c.title == "Data Mining And Machine Learning"
    assert c.credits_min == 3
    assert c.description.startswith("This course will introduce")
    assert "1." not in c.description
    assert c.extra["Learning Outcomes"].startswith("1. Identify")


def test_acalog_course_ferris_bold_label_without_colon():
    c = course("ARTI 350", "acalog_ferris_arti350.html")
    assert c.prereq_text.startswith("ISIN 306")
    assert c.extra["Department"] == "AFIS"


def test_acalog_course_dsu_inline_prereq():
    c = course("CSC 247", "acalog_dsu_csc247.html")
    assert c.prereq_text == "CSC 150 and MATH 201"
    assert c.credits_min == 3
    assert "Prerequisite" not in c.description


def test_acalog_course_capitol_hours_without_colons():
    c = course("AIT 201", "acalog_captech_ait201.html")
    assert c.credits_min == 3
    assert c.prereq_text == "MA 128"
    assert c.extra["Class Hours"] == "3"


def test_acalog_course_harding_plain_labels():
    c = course("COMP 2200", "acalog_harding_comp2200.html")
    assert c.prereq_text == "COMP 1500 or COMP 1700"
    assert c.extra["Term Offered"] == "Spring of even years"
