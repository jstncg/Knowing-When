import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from app.sources import sec
from app.sources.sec import Filing, OfficerEvent, TimelineEvent

FIXTURES = Path(__file__).parent / "fixtures" / "sec"

FILING = Filing(cik="0000123456", company="Acme Widgets, Inc.", accession="0001193125-25-000001", form="8-K",
                filing_date="2025-03-03", report_date="2025-02-28", accepted_at="2025-03-03T16:05:23.000Z",
                items=["2.01", "4.01", "5.02", "9.01"], primary_doc="d1.htm")

# Synthetic 8-K in the wording real filings use; the recorded fixtures below are the real ones.
BODY = """<html><head><title>8-K</title></head><body>
<p>FORM 8-K Date of Report (Date of earliest event reported): February 28, 2025</p>
<p>Item 2.01 Completion of Acquisition or Disposition of Assets.</p>
<p>On February 28, 2025, Acme Widgets, Inc. (the "Company") completed its previously announced acquisition of Bolt Co.
pursuant to the Agreement and Plan of Merger dated January 5, 2025.</p>
<p>Item 4.01 Changes in Registrant's Certifying Accountant.</p>
<p>On February 26, 2025, the Audit Committee of the Board of Directors dismissed Old Auditors LLP as the Company's
independent registered public accounting firm.</p>
<p>Item 5.02 Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain Officers;
Compensatory Arrangements of Certain Officers.</p>
<p>On February 27, 2025, John A. Smith, the Company's Senior Vice President, Corporate Controller and Chief Accounting
Officer, notified the Company of his decision to resign, effective March 31, 2025, to pursue another opportunity.
Mr. Smith's resignation was not the result of any disagreement with the Company on any matter relating to its
operations, policies or practices. The Board of Directors appointed Jane Q. Doe as Vice President and Chief
Accounting Officer (principal accounting officer), effective April 1, 2025. Ms. Doe, age 44, joined the Company in 2019.</p>
<p>Item 9.01 Financial Statements and Exhibits.</p></body></html>"""


def test_events_from_synthetic_8k():
    events = sec.events_from_8k(FILING, BODY)
    by_type = {e.event_type: e for e in events}
    assert set(by_type) == {"acquisition_closed", "auditor_change", "officer_departure", "officer_appointment"}
    text = sec.html_text(BODY)
    for e in events:
        assert e.tier == 1 and e.extractor == "sec_8k_v1" and e.date_precision == "day"
        assert e.observed_at == "2025-03-03T16:05:23+00:00"
        assert e.source_url == FILING.url and len(e.source_version_hash) == 64
        assert e.quote in text and len(e.quote) <= 300
    assert by_type["acquisition_closed"].subject_id == "0000123456"
    assert by_type["acquisition_closed"].event_date == "2025-02-28"
    assert by_type["auditor_change"].event_date == "2025-02-26"
    gone = by_type["officer_departure"]
    assert isinstance(gone, OfficerEvent)
    assert gone.subject_id == "john-smith|acme-widgets-inc"
    assert gone.title == "Senior Vice President, Corporate Controller and Chief Accounting Officer"
    assert gone.event_date == "2025-03-31" and gone.reason == "to pursue another opportunity"
    hired = by_type["officer_appointment"]
    assert hired.person_name == "Jane Q. Doe" and hired.title == "Vice President and Chief Accounting Officer"
    assert hired.event_date == "2025-04-01"


@pytest.mark.parametrize("sentence, expected", [
    ("On May 1, 2025, Robert Jones, Chief Financial Officer, will retire effective June 30, 2025.",
     [("Robert Jones", "Chief Financial Officer", True)]),
    ("Effective immediately, Maria Lopez was appointed Treasurer and Corporate Controller of the Company.",
     [("Maria Lopez", "Treasurer and Corporate Controller", False)]),
    ("The Company announced that Wei Chen will step down as Principal Accounting Officer and Ana Ruiz will serve as "
     "Principal Accounting Officer.", [("Wei Chen", "Principal Accounting Officer", True),
                                     ("Ana Ruiz", "Principal Accounting Officer", False)]),
    ("The Board approved an amended bonus plan for executive officers.", []),
    # Shapes seen in the recorded June 2025 filings.
    ("On June 18, 2025, Peter R. Wall provided notice that he will resign as senior vice president, controller and "
     "chief accounting officer of Acme.",
     [("Peter R. Wall", "senior vice president, controller and chief accounting officer", True)]),
    ("On May 27, 2025, Alison Vasquez notified the Company of her decision to resign from her role of Senior Vice "
     "President, Chief Accounting Officer.", [("Alison Vasquez", "Senior Vice President, Chief Accounting Officer", True)]),
    ("The Company has appointed Shad E. Evans, Senior Vice President of Financial Operations, to serve as interim "
     "Chief Accounting Officer.", [("Shad E. Evans", "Chief Accounting Officer", False)]),
    ("Karen L. Sedgwick will continue to serve as a director of the Company.", []),
    ("Ms. Taylor was promoted to Director of SEC Reporting in May 2021.", []),
    ("Jane Doe resigned as Chief Accounting Officer. Ms. Doe’s successor will be appointed as Chief Accounting Officer.",
     [("Jane Doe", "Chief Accounting Officer", True)]),
    ("The Board appointed John Roe as Chief Accounting Officer, succeeding Jane Doe.",
     [("John Roe", "Chief Accounting Officer", False)]),
    ("Acme Widgets announced that Jane Doe resigned as Corporate Controller.", [("Jane Doe", "Corporate Controller", True)]),
    ("The Company's Chief Accounting Officer, José Núñez, resigned effective May 30, 2025.",
     [("José Núñez", "Chief Accounting Officer", True)]),
    # Employers in a biography are not people (shapes from the first real Controller key).
    ("The Board appointed Ann Bell as Controller. Prior to joining the Company, Ms. Bell served as Controller of "
     "Crimson Midstream, LLC.", [("Ann Bell", "Controller", False)]),
    ("Before that, Mr. Kaye was appointed Vice President and Chief Accounting Officer of Extraction Oil & Gas, Inc.", []),
    ("Prior to joining Extraction Oil & Gas, Inc., he was named Chief Accounting Officer.", []),
    ("He was previously appointed Senior Vice President and Corporate Controller at a NYSE-listed Fortune 500 company.", []),
    # Giving up a designation, or continuing in another office, is not a departure.
    ("Effective May 27, 2025, Carla Lee will no longer serve as principal financial officer and principal accounting "
     "officer of the Company. Ms. Lee will continue to serve as the Company's Chief Accounting Officer.", []),
    ("In connection with Mark Otto's appointment as Chief Accounting Officer, Dana Fox, Chief Financial Officer, "
     "will no longer serve as principal accounting officer.", [("Mark Otto", "Chief Accounting Officer", False)]),
    ("Jane Doe resigned as Chief Accounting Officer and will continue to serve as Chief Accounting Officer until "
     "March 31, 2025.", [("Jane Doe", "Chief Accounting Officer", True)]),
    ("Jane Doe resigned as Chief Accounting Officer, and John Roe will continue to serve as Chief Financial Officer.",
     [("Jane Doe", "Chief Accounting Officer", True)]),
    ("John Roe will continue to serve as Chief Financial Officer following Jane Doe's resignation as Chief Accounting "
     "Officer.", [("Jane Doe", "Chief Accounting Officer", True)]),
    ("John Roe will no longer serve as principal financial officer, and Jane Doe ceased to serve as Controller of the "
     "Company.", [("Jane Doe", "Controller", True)]),
    ("John Roe resigned as Controller, and Jane Doe will no longer serve as principal accounting officer.",
     [("John Roe", "Controller", True)]),
    ("Jane Doe resigned as Chief Accounting Officer and will continue to serve as Chief Accounting Officer of Acme "
     "Widgets, Inc. until March 31, 2025.", [("Jane Doe", "Chief Accounting Officer", True)]),
    ("John Roe will step down from his position as Interim Chief Financial Officer and will remain Chief "
     "Accounting Officer.", []),
    ("John Roe will step down from his position as Interim CFO, effective June 22, 2026, and will remain Chief "
     "Accounting Officer.", []),
    ("Jane Doe will resign as CFO, effective December 31, 2026, and will continue to serve as Chief Financial Officer "
     "to assist with the transition.", [("Jane Doe", "Chief Financial Officer", True)]),
    ("Jane Doe will retire as EVP and CFO and will remain chief financial officer to facilitate an orderly "
     "transition.", [("Jane Doe", "chief financial officer", True)]),
    ("Jane Doe will resign as Chief Financial Officer and Treasurer and will continue to serve as Treasurer.", []),
    ("Jane Doe, Executive Vice President and Chief Financial Officer, notified the Company of her intention to retire "
     "and will remain Chief Financial Officer to assist with the transition.",
     [("Jane Doe", "Executive Vice President and Chief Financial Officer", True)]),
    ("Jane Doe will step down as Chief Accounting Officer and will continue to serve as Chief Administrative Officer.", []),
    ("Jane Doe, Vice President, Controller, notified the Board of Directors of her intention to retire and will "
     "continue to serve as Controller to assist with the transition.", [("Jane Doe", "Vice President, Controller", True)]),
    ("Jane Doe notified the Board of Directors of her decision to resign as Controller. Ms. Doe will continue to serve "
     "as Controller to assist with the transition.", [("Jane Doe", "Controller", True)]),
    ("John Roe will cease serving as the Company's Interim Chief Financial Officer and will continue serving as "
     "Vice President, Chief Accounting Officer.", []),
    ("Jane Doe will step down as Interim Chief Financial Officer and will continue to be the Company's Chief "
     "Accounting Officer.", []),
    ("Jane Doe resigned as Chief Financial Officer and will continue serving as Chief Financial Officer to ensure an "
     "orderly transition.", [("Jane Doe", "Chief Financial Officer", True)]),
    ("Jane Doe resigned; her remaining Chief Accounting Officer duties pass to the continuing Chief Financial Officer.",
     [("Jane Doe", "Chief Accounting Officer", True)]),
    ("Jane Doe will no longer serve as principal accounting officer upon her resignation from the Company.",
     [("Jane Doe", "principal accounting officer", True)]),
])
def test_officer_mentions(sentence, expected):
    got = [(m["name"], m["title"], m["departure"]) for m in sec.officer_mentions(sentence, "Acme Widgets, Inc.")]
    assert got == expected


def test_departure_reason_can_follow_the_departure_sentence():
    section = ("On June 2, 2025, Brent Rhodes notified the Company of his decision to resign from the position of Chief "
               "Accounting Officer effective June 27, 2025. Mr. Rhodes is leaving the Company to pursue another opportunity.")
    assert [m["reason"] for m in sec.officer_mentions(section)] == ["to pursue another opportunity"]


@pytest.mark.parametrize("paragraph", [
    "On June 2, 2025, the Company completed the sale of its Widgets business to Buyer Inc.",
    "On May 29, 2025, the Company amended the Asset Purchase Agreement with Big Co. (the “Buyer”) pursuant to which "
    "the Buyer purchased additional assets. The purchase price was $1.5 million paid at closing.",
    "On June 2, 2025, the Company completed the transaction in which the Buyer acquired all of the equity of Sub Co.",
])
def test_disposition_is_not_an_acquisition(paragraph):
    body = f"<p>Item 2.01 Completion of Acquisition or Disposition of Assets.</p><p>{paragraph}</p>"
    assert sec.events_from_8k(FILING, body) == []


def test_stated_date_prefers_effective_then_first_then_default():
    assert sec.stated_date("On May 1, 2025, X resigned effective as of June 30, 2025.", "2025-05-02") == "2025-06-30"
    assert sec.stated_date("On May 1, 2025, X resigned.", "2025-05-02") == "2025-05-01"
    assert sec.stated_date("X resigned.", "2025-05-02") == "2025-05-02"
    assert sec.stated_date("On February 30, 2025, X resigned.", "2025-05-02") == "2025-05-02"
    # "with effect", and dates long before the report are history (a tenure start), not the event.
    assert sec.stated_date("On January 15, 2025, X resigned with effect as of January 31, 2025.", "2025-01-15") == "2025-01-31"
    assert sec.stated_date("X, our Principal Accounting Officer since July 1, 2005, will retire.", "2025-08-12") == "2025-08-12"
    assert sec.stated_date("Under the agreement dated March 3, 2025, on June 2, 2025 the merger closed.", "2025-06-02") == "2025-06-02"


def test_quote_is_exact_substring_and_short():
    sentence = "Alpha " * 40 + "resigned " + "omega " * 40
    quote = sec.quote_of(sentence.strip(), sentence.index("resigned"))
    assert quote in sentence and len(quote) <= 300 and "resigned" in quote


def test_item_sections_use_first_heading_only():
    text = "Item 5.02 Departure. Alice left. Item 9.01 Exhibits. See Item 5.02 above."
    assert sec.item_sections(text) == {"5.02": "Departure. Alice left.", "9.01": "Exhibits. See Item 5.02 above."}
    assert sec.sentences("Departure of Directors or Certain Officers. Alice left.") == ["Alice left."]
    text = "Item 1.01 Entry. See Item 2.01 below, which is incorporated here. Item 2.01 Completion. X was acquired."
    assert sec.item_sections(text) == {"1.01": "Entry. See Item 2.01 below, which is incorporated here.",
                                       "2.01": "Completion. X was acquired."}


def test_person_id_ignores_initials_punctuation_and_suffixes():
    # "Jane Q. Doe, Jr." in one 8-K and "Jane Q Doe" in another are one person.
    ids = {sec.person_id(n, "Acme Widgets, Inc.") for n in ["Jane Q. Doe, Jr.", "Jane Q Doe"]}
    assert ids == {"doe-jane|acme-widgets-inc"}


def test_offline_client_reads_cache_and_never_fetches(tmp_path):
    client = sec.EdgarClient(cache_dir=tmp_path, offline=True)
    with pytest.raises(sec.SecError):
        client.get("https://data.sec.gov/submissions/CIK0000000001.json")
    url = "https://data.sec.gov/submissions/CIK0000000002.json"
    client._path(url).write_text(json.dumps({"url": url, "body": '{"ok": 1}'}))
    assert client.get_json(url) == {"ok": 1} and client.fetched == 0


def test_report_events_quote_the_filing():
    ten_k = Filing(**{**FILING.__dict__, "form": "10-K", "items": [], "report_date": "2024-12-31"})
    index = ("<div>Filing Date</div><div>2025-03-03</div><div>Accepted</div><div>2025-03-03 11:05:23</div>"
             "<div>Period of Report</div><div>2024-12-31</div>")
    e = sec.event_from_report(ten_k, index, ten_k.index_url)
    assert (e.event_type, e.event_date, e.source_url) == ("annual_report_filed", "2025-03-03", ten_k.index_url)
    assert e.quote == "Filing Date 2025-03-03 Accepted 2025-03-03 11:05:23 Period of Report 2024-12-31"
    late = Filing(**{**FILING.__dict__, "form": "NT 10-K", "items": []})
    body = ("<p>PART II — RULES 12b-25(b) AND (c) If the subject report could not be filed without unreasonable "
            "effort, the box is checked.</p><p>PART III — NARRATIVE State below in reasonable detail why the report "
            "could not be filed within the prescribed time period. The Registrant is unable to complete its annual "
            "report without unreasonable effort.</p>")
    e = sec.event_from_report(late, body)
    assert e.event_type == "late_filing"
    assert e.quote == "The Registrant is unable to complete its annual report without unreasonable effort."


@pytest.mark.skipif(not (FIXTURES / "expected.json").exists(), reason="no recorded filings")
def test_recorded_filings_replay_offline():
    """Real filings, recorded from EDGAR in June 2025."""
    client = sec.EdgarClient(cache_dir=FIXTURES, offline=True)
    expected = json.loads((FIXTURES / "expected.json").read_text())
    assert expected, "no recorded filings"
    for case in expected:
        filing = Filing(**case["filing"])
        events = [e.model_dump() for e in sec.filing_events(client, filing)]
        assert events == case["events"]
        for e in events:
            TimelineEvent.model_validate(e)
            assert e["tier"] == 1 and len(e["quote"]) <= 300
    assert client.fetched == 0
