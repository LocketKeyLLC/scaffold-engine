"""§17.1975 — the research reads an XML document as its elements (namespace, element paths, a repeated element by
its first item), and the SEC seeds carry one 13F-HR filing: its folder's file list, its cover page and its
information table.

Live (§17.1947, 2026-11-19): "Not found: a loadable page stating the 13F information-table XML fields" -- 5b4880c0's
SEC audit kept `nameOfIssuer`, `titleOfClass`, `cusip`, `value`, `shrsOrPrnAmt/sshPrnamt` and the namespace
UNVERIFIED. Measured 2026-11-21 through the engine's own fetch: the spec PDFs 404, the 13F data-sets page states no
field, and a real filing's information table loaded (200, 45 kB) but trafilatura kept the values and dropped every
tag (`ALLY FINL INC / COM / 02005N100`, no `nameOfIssuer`)."""
from __future__ import annotations

from app.modules import engine_plan as ep
from app.modules import research_agent as ra

INFO_TABLE = """<informationTable xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>LANTERN WORKS CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>123456789</cusip>
    <value>1500000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>30000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority>
      <Sole>30000</Sole>
      <Shared>0</Shared>
      <None>0</None>
    </votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>BRIGHT HARBOR INC</nameOfIssuer>
    <titleOfClass>CL A</titleOfClass>
    <cusip>987654321</cusip>
    <value>720000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>12000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <putCall>Put</putCall>
    <investmentDiscretion>DFND</investmentDiscretion>
    <votingAuthority>
      <Sole>0</Sole>
      <Shared>12000</Shared>
      <None>0</None>
    </votingAuthority>
  </infoTable>
</informationTable>
"""


def test_a_13f_information_table_is_its_namespace_and_fields():
    text, date = ra._extract_text_and_date(INFO_TABLE)
    assert date == ""
    head, *lines = text.splitlines()
    assert head == ("XML document `informationTable`, namespace "
                    "`http://www.sec.gov/edgar/document/thirteenf/informationtable` (infoTable ×2); its elements "
                    "(a repeated element read by its first item):"), head
    assert lines[:4] == ["informationTable.infoTable[].nameOfIssuer: e.g. 'LANTERN WORKS CORP'",
                         "informationTable.infoTable[].titleOfClass: e.g. 'COM'",
                         "informationTable.infoTable[].cusip: e.g. '123456789'",
                         "informationTable.infoTable[].value: e.g. '1500000'"], lines
    for path in ("shrsOrPrnAmt.sshPrnamt", "shrsOrPrnAmt.sshPrnamtType", "investmentDiscretion",
                 "votingAuthority.Sole", "votingAuthority.Shared", "votingAuthority.None"):
        assert any(x.startswith(f"informationTable.infoTable[].{path}: ") for x in lines), path
    assert len(lines) == 10                       # one line per path: the second holding is not read again


def test_an_attribute_a_declaration_and_a_nested_repeat_are_read():
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n<edgarSubmission schemaVersionNote="X0202">'
            "<headerData><submissionType>13F-HR</submissionType></headerData>"
            "<otherManagers><otherManager><name>A</name></otherManager><otherManager><name>B</name></otherManager>"
            "</otherManagers></edgarSubmission>")
    text = ra._non_html_text(body)
    assert text is not None and text.startswith("XML document `edgarSubmission`;")
    assert "edgarSubmission@schemaVersionNote: e.g. 'X0202'" in text
    assert "edgarSubmission.headerData.submissionType: e.g. '13F-HR'" in text
    assert "edgarSubmission.otherManagers.otherManager[].name: e.g. 'A'" in text and "'B'" not in text


def test_an_html_page_and_a_broken_or_hostile_body_are_not_read_as_xml():
    assert ra._non_html_text("<!doctype html><html><body><p>x</p></body></html>") is None
    assert ra._non_html_text('<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>') is None   # XHTML too
    assert ra._non_html_text("<p>an HTML fragment<br></p>") is None                              # does not parse
    bomb = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
            "<r>&b;</r>")
    assert ra._non_html_text(bomb) is None                                       # defusedxml refuses entities


def test_the_sec_seeds_carry_one_13f_filing():
    sec = next(a for a in ep._KNOWN_APIS if a["key"] == "SEC_EDGAR")["docs"]
    folder = "https://www.sec.gov/Archives/edgar/data/1067983/000119312526463399/"
    assert [u for u in sec if u.startswith(folder)] == [folder + "index.json", folder + "primary_doc.xml",
                                                        folder + "60471.xml"]
