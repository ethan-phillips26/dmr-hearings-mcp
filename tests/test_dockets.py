from dmr_hearings.pipeline.dockets import DOCKET_LINK_RE, applicant_from, parse_docket


def test_applicant_from_application():
    desc = "Application of Kraken Operating, LLC for an order amending the applicable orders"
    assert applicant_from(desc) == "Kraken Operating, LLC"


def test_applicant_from_continued_and_commission_motion():
    assert applicant_from("(Continued) Application of Murex Petroleum Corporation pursuant to NDAC 43-02") == (
        "Murex Petroleum Corporation"
    )
    assert applicant_from("Commission's own motion to consider flaring policy") is None


def test_docket_link_regex_reads_date_and_supplement():
    m = DOCKET_LINK_RE.search('<a href="/oilgas/dockets/2026/docket082626s1.pdf">')
    assert m.groups() == ("08", "26", "26", "s1")


DOCKET = """\
NDIC Docket for 8/26/2026
Case No.         Application of Kraken Operating, LLC for an order amending the
33085:           applicable orders for the Sheep Butte-Bakken Pool, McKenzie
Notice to        County, ND
CONTINUE
Case No.         Application of Murex Petroleum Corporation for an order
33086:           pooling all interests in a spacing unit
Administrative Docket
Case No.         Application of Zavanna, LLC for an order authorizing flaring
33090:           of gas from the Lone Tree well
Page 1 of 1
"""


def test_parse_docket_splits_cases_and_separates_margin_notes():
    cases = parse_docket(DOCKET)
    assert [c["case_number"] for c in cases] == ["33085", "33086", "33090"]
    first = cases[0]
    assert first["applicant"] == "Kraken Operating, LLC"
    assert first["notes"] == "Notice to CONTINUE"
    assert "Sheep Butte-Bakken Pool" in first["description"]
    assert [c["section"] for c in cases] == ["main", "main", "administrative"]
