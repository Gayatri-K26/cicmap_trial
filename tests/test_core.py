from pathlib import Path

import pytest

from cicmap import db, sheet
from cicmap.adapters import get_adapter, select_adapter
from cicmap.models import CurriculumEntry
from cicmap.prereqs import regex_parse
from cicmap.text import normalize_code, parse_credits

FIX = Path(__file__).parent / "fixtures"


def read(name):
    return (FIX / name).read_text()


# --- text helpers -------------------------------------------------------------

@pytest.mark.parametrize("raw,code", [
    ("CS 380", "CS 380"), ("CS 380", "CS 380"), ("CPS*2800", "CPS 2800"),
    ("cps-3830", "CPS 3830"), ("ECEC 487", "ECEC 487"), ("no code here", None),
])
def test_normalize_code(raw, code):
    assert normalize_code(raw) == code


def test_parse_credits():
    assert parse_credits("3.0 Credits") == (3.0, 3.0)
    assert parse_credits("1-4") == (1.0, 4.0)
    assert parse_credits(None) == (None, None)


def test_regex_prereqs_and_of_or():
    groups = regex_parse("(CS 331 or CS 401) and (MATH 474 or MATH 475)")
    assert [[o["code"] for o in g] for g in groups] == [["CS 331", "CS 401"], ["MATH 474", "MATH 475"]]


# --- sheet sync ---------------------------------------------------------------

CSV1 = ("Institution,Program,Latest Catalog Link\r\n"
        "A University,BS AI,https://a.edu/ai\r\n"
        "B College,BS ML,https://b.edu/ml\r\n")


@pytest.fixture
def conn():
    return db.connect(Path(":memory:"))


def test_sync_add_change_remove(conn):
    r = sheet.sync(conn, csv_text=CSV1)
    assert len(r.added) == 2
    # Identical file: short-circuits on the file hash.
    r = sheet.sync(conn, csv_text=CSV1)
    assert not r.added and not r.changed and r.unchanged == 2
    # B renamed, A removed, C added.
    csv2 = ("Institution,Program,Latest Catalog Link\r\n"
            "B College,BS Machine Learning,https://b.edu/ml\r\n"
            "C Tech,BS AI,https://c.edu/ai\r\n")
    r = sheet.sync(conn, csv_text=csv2)
    assert r.added == ["C Tech - BS AI"]
    assert r.changed == ["B College - BS Machine Learning"]
    assert r.removed == ["A University - BS AI"]
    assert conn.execute("SELECT active FROM program_sources WHERE catalog_url='https://a.edu/ai'").fetchone()[0] == 0


def test_sync_ttl_skips(conn):
    sheet.sync(conn, csv_text=CSV1)
    assert sheet.sync(conn).skipped_reason  # no download attempted within the TTL


# --- parsers ------------------------------------------------------------------

def test_courseleaf_program_drexel():
    url = "https://catalog.drexel.edu/undergraduate/computer-information-sciences/artificialintellicenceandmachinelearningbs/"
    a, _ = select_adapter(url, read("courseleaf_drexel_program.html"))
    assert a.name == "courseleaf"
    p = a.parse_program(url, read("courseleaf_drexel_program.html"))
    codes = {e.code: e for e in p.entries}
    assert len(p.entries) > 50
    assert codes["CS 380"].title == "Artificial Intelligence"
    assert codes["CS 380"].requirement_group == "Core Requirements"
    assert codes["ENGR 131"].is_alternative
    assert p.college == "School of Computer and Information Sciences"


def test_courseleaf_course_iit():
    c = get_adapter("courseleaf").parse_course(CurriculumEntry(code="CS 480"), "u", read("courseleaf_iit_cs480.xml"))
    assert c.title == "Introduction to Artificial Intelligence"
    assert c.credits_min == 3
    assert "CS 331" in c.prereq_text


def test_courseleaf_course_drexel():
    c = get_adapter("courseleaf").parse_course(CurriculumEntry(code="CS 380"), "u", read("courseleaf_drexel_cs380.xml"))
    assert c.title == "Artificial Intelligence"
    assert c.prereq_text.startswith("CS 260")
    assert c.restrictions


def test_smartcatalog_program_kean():
    url = "https://kean.smartcatalogiq.com/en/2026-2027/undergraduate-catalog/x/artificial-intelligence-b-s/"
    a, why = select_adapter(url, read("smartcatalog_kean_program.html"))
    assert a.name == "smartcatalog" and why == "domain"
    p = a.parse_program(url, read("smartcatalog_kean_program.html"))
    assert p.catalog_year == "2026-2027"
    assert p.department == "Department of Computer Science and Technology"
    assert any(e.code == "CPS 3830" for e in p.entries)


def test_smartcatalog_course_kean():
    c = get_adapter("smartcatalog").parse_course(CurriculumEntry(code="CPS 3830"), "u",
                                                 read("smartcatalog_kean_cps3830.html"))
    assert c.title == "MACHINE LEARNING FOUNDATIONS"
    assert c.credits_min == 3
    assert c.prereq_text.startswith("CPS*2800")


def test_smartcatalog_prereq_in_description():
    from cicmap.adapters.smartcatalog import _from_description
    d = "Hands-on ML. Students must achieve a minimum grade of C. Prerequisite: DATA 325 with a grade of C or higher. (3)"
    assert _from_description(d, r"prerequisites?") == "DATA 325 with a grade of C or higher"
