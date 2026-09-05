---
id: "cfr-14-part-291-Appendix-A-to-Subpart-E-of-Part-291"
type: "appendix"
citation: "14 CFR Part 291, Appendix-A-to-Subpart-E-of-Part-291"
title_number: 14
part: 291
appendix: "Appendix-A-to-Subpart-E-of-Part-291"
source: "ecfr"
source_version: "2026-09-03"
canonical_hash: "sha256:3c57a61fcca842757b395d945f5888344a34486694b4eaae228b8bd1b20a0e7f"
generated: true
title: "Appendix A to Subpart E of Part 291—Instructions to U.S. Air Carriers for Reporting Traffic and Capacity Data on Schedule T-100"
aliases:
  - "Appendix A to Subpart E of Part 291—Instructions to U.S. Air Carriers for Reporting Traffic and Capacity Data on Schedule T-100"
tags:
  - "far"
  - "regulation"
---

# Appendix A to Subpart E of Part 291—Instructions to U.S. Air Carriers for Reporting Traffic and Capacity Data on Schedule T-100

> [!info] Source
> eCFR Title 14, issue 2026-09-03 — [view on eCFR](https://www.ecfr.gov/on/2026-09-03/title-14/part-291)

## Official Text

(a) Format of reports—(1) Automatic Data Processing (ADP) magnetic tape. Refer to paragraph (d) of this appendix for instructions pertaining to mainframe and minicomputer reporting. The Department will issue “Accounting and Reporting Directives” to make necessary technical changes to these T-100 instructions. Technical changes which are minor in nature do not require public notice and comment.

(2) Microcomputer diskette—(i) Optional specification. If an air carrier desires to use its personal computers (PC's), rather than mainframe or minicomputers to prepare its data submissions, the following specifications for filing data on diskette media apply.

(ii) Reporting medium. Microcomputer ADP data submission of T-100 information must be on IBM compatible disks. Carriers wishing to use a different ADP procedure must obtain written approval to do so from the BTS Assistant Director—Airline Information. Requests for approval to use alternate methods must disclose and describe the proposed data transmission methodology. Refer to paragraph (i) of this appendix for microcomputer record layouts.

(iii) Microcomputer file characteristics. The files will be created in ASCII delimited format, sometimes called Data Interchange Format (DIF). This form of recording data provides for variable length fields (data elements) which, in the case of alphabetic data, are enclosed by quotation marks (“) and separated by a comma (,) or tab. Numeric data elements that are recorded without editing symbols are also separated by a comma (,) or tab. The data are identified by their juxtaposition within a given record. Therefore, each record must contain the exact number of data elements, all of which must be juxtapositionally correct. Personal computer software including most spreadsheets, data base management programs, and BASIC are capable of producing files in this format.

(b) Filing date for reports. The reports must be received at BTS within 30 days following the end of each reporting period.

(c) Address for filing. Data Administration Division, RTS-42, Office of Airline Information, Bureau of Transportation Statistics, U.S. Department of Transportation, 1200 New Jersey Avenue SE, Washington, DC 20590-0001.

(d) ADP format for magnetic tape—(1) Magnetic tape specifications. IBM compatible 9-track EBCDIC recording. Recording density of 6250 or 1600 bpi. The order of recorded information is:

(i) Volume label.

(ii) Header label.

(iii) Data records.

(iv) Trailer label.

(2) \[Reserved]

(e) External tape label information. (1) Carrier name.

(2) Report date.

(3) File identification.

(4) Carrier address for return of tape reel.

(f) Standards. It is the policy of the Department to be consistent with the American National Standards Institute and the Federal Standards Activity in all data processing and telecommunications matters. It is our intention that all specifications in this application are in compliance with standards promulgated by these organizations.

(g) Volume, header, and trailer label formats—(1) Use standard IBM label formats. The file identifier field of the header labels should be “T-100.SYSTEM”.

(h) Magnetic tape record layouts for T-100—(1) Nonstop segment record layout.

<table>
<thead>
<tr><th>Field No.</th><th>Positions</th><th>Mode</th><th>Description</th></tr>
</thead>
<tbody>
<tr><td>1</td><td>1</td><td>1T</td><td>Record type code (S = nonstop segment).</td></tr>
<tr><td>2</td><td>2-6</td><td>5T</td><td>Carrier entity code.</td></tr>
<tr><td>3</td><td>7-12</td><td>6T</td><td>Report date (YYYYMM).</td></tr>
<tr><td>4</td><td>13-15</td><td>3T</td><td>Origin airport code.</td></tr>
<tr><td>5</td><td>16-18</td><td>3T</td><td>Destination airport code.</td></tr>
<tr><td>6</td><td>19</td><td>1T</td><td>Service class code (F, G, L, N, P or R).</td></tr>
<tr><td>7</td><td>20-23</td><td>4T</td><td>Aircraft type code.</td></tr>
<tr><td>8</td><td>24-28</td><td>5N</td><td>Revenue departures performed (F, G, L, N, P, R510).</td></tr>
<tr><td>9</td><td>29-38</td><td>10N</td><td>Available capacity payload (lbs) (F, G, L, N, P, R270).</td></tr>
<tr><td>10</td><td>39-45</td><td>7N</td><td>Available seats (F, L, N310).</td></tr>
<tr><td>11</td><td>46-52</td><td>7N</td><td>Passengers transported (F, L, N130).</td></tr>
<tr><td>12</td><td>53-62</td><td>10N</td><td>Rev freight transported (F, G, L, N, P, R237) (in lbs).</td></tr>
<tr><td>13</td><td>63-72</td><td>10N</td><td>Revenue mail transported (F, G, L, N, P, R239) (in lbs).</td></tr>
<tr><td>14</td><td>73-77</td><td>5N</td><td>Revenue aircraft departures scheduled (F, G520).</td></tr>
<tr><td>15</td><td>78-87</td><td>10N</td><td>Rev hrs, ramp-to-ramp (F, G, L, N, P, R630) (in minutes).</td></tr>
<tr><td>16</td><td>88-97</td><td>10N</td><td>Rev hrs, airborne (F, G, L, N, P, R610) (in minutes).</td></tr>
</tbody>
<tfoot>
<tr><td colspan="4">T = Text.</td></tr>
<tr><td colspan="4">N = Numeric.</td></tr>
</tfoot>
</table>

(2) On-flight market record layout.

<table>
<thead>
<tr><th>Field No.</th><th>Positions</th><th>Mode</th><th>Description</th></tr>
</thead>
<tbody>
<tr><td>1</td><td>1</td><td>1T</td><td>Record type: M = on-flight market record.</td></tr>
<tr><td>2</td><td>2-6</td><td>5T</td><td>Carrier entity code.</td></tr>
<tr><td>3</td><td>7-12</td><td>4T</td><td>Report date (YYYYMM).</td></tr>
<tr><td>4</td><td>13-15</td><td>3T</td><td>Origin airport code.</td></tr>
<tr><td>5</td><td>16-18</td><td>3T</td><td>Destination airport code.</td></tr>
<tr><td>6</td><td>19</td><td>1T</td><td>Service class code (F, G, L, N, P or R).</td></tr>
<tr><td>7</td><td>20-26</td><td>7N</td><td>Total passengers in market (F, L, N110).</td></tr>
<tr><td>8</td><td>27-36</td><td>10N</td><td>Rev freight in market (F, G, L, N, P, R217) (in lbs).</td></tr>
<tr><td>9</td><td>37-46</td><td>10N</td><td>Revenue mail in market (F, G, L, N, P, R219) (in lbs).</td></tr>
</tbody>
<tfoot>
<tr><td colspan="4">T = Text.</td></tr>
<tr><td colspan="4">N = numeric.</td></tr>
</tfoot>
</table>

(i) Record layouts for microcomputer diskettes. The record layouts for diskette are generally identical to those shown for magnetic tape, with the exception that delimiters (quotation marks, tabs and commas) are used to separate fields. It is necessary that the order of fields be maintained in all records.

(1) File characteristics. The files will be created in ASCII delimited format, sometimes called Data Interchange Format (DIF). This form of recording data provides for variable length fields (data elements) which, in the case of alphabetic data, are enclosed by quotation marks (”) and separated by a comma (,) or tab. Numeric data elements that are recorded without editing symbols are also separated by a comma (,) or tab. The data are identified by their juxtaposition within a given record. Therefore, it is critical that each record contain the exact number of data elements, all of which must be juxtapositionally correct. PC software including most spreadsheets, data base management programs, and BASIC produce minidisk files in this format.

(2) File naming conventions for diskettes. For microcomputer reports, each record type should be contained in a separate DOS file on the same physical diskette. The following DOS naming conventions should be followed:

(i) Record type S = SEGMENT.DAT

(ii) Record type M = MARKET.DAT

## Source Notes

**Citations:**

\[Doc. No. DOT-OST-2014-0140, 84 FR 15933, Apr. 16, 2019]
