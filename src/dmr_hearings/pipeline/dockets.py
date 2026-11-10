"""Download the official NDIC hearing dockets and save each listed case to hearings.db.

Usage:
    python -m dmr_hearings.pipeline.dockets                # every year that has a scraped hearing
    python -m dmr_hearings.pipeline.dockets --year 2026    # one year
    python -m dmr_hearings.pipeline.dockets --refresh      # download dockets again even if already saved
"""

import argparse
import io
import re
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from pypdf import PdfReader

from .. import db

SITE = "https://www.dmr.nd.gov"
INDEX_URL = f"{SITE}/oilgas/docketindex.asp"
HEADERS = {"User-Agent": "Mozilla/5.0 (DMR hearing search)"}

# /oilgas/dockets/2026/docket082626.pdf, ...082626s1.pdf, ...121523s.pdf
DOCKET_LINK_RE = re.compile(r"/oilgas/dockets/\d{4}/docket(\d{2})(\d{2})(\d{2})(s\d*)?\.pdf", re.I)

# Page headers and footers repeated on every page.
FURNITURE_RE = re.compile(r"Listen to the hearings live|dmr\.nd\.gov/oilgas|NDIC Docket for|Page \d+ of \d+", re.I)

CASE_LINE_RE = re.compile(r"^(\s*Case No\.?\s+)(.*)$")
NUMBER_LINE_RE = re.compile(r"^(\s*(\d{5}):\s*)(.*)$")
# "Application of Kraken Operating, LLC for an order", "(Continued) Application of X pursuant to NDAC ...",
# "Application of X requesting ...", "(Reconsideration) Application of X as provided for in ..."
APPLICANT_RE = re.compile(
    r"^(?:\((?:Continued|Reconsideration)\)\s*)?(?:Application|Petition) of (.+?),?\s+"
    r"(?:for|to|requesting|pursuant|as provided|provided|in accordance|seeking|under|affirming|an? order|the order)\b",
    re.I,
)


def fetch(url: str, data: dict | None = None) -> bytes:
    body = urllib.parse.urlencode(data).encode() if data else None
    request = urllib.request.Request(url, data=body, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def list_dockets(year: int) -> list[tuple[str, str]]:
    """Return (hearing_date, url) for every docket PDF listed for a year, main docket before supplements."""
    html = fetch(INDEX_URL, {"SELECTYEAR": year, "B1": "Get Index"}).decode("latin-1")
    found = {}
    for m in DOCKET_LINK_RE.finditer(html):
        month, day, yy, supplement = m.groups()
        hearing_date = f"20{yy}-{month}-{day}"
        found[m.group(0)] = (hearing_date, supplement or "")
    ordered = sorted(found.items(), key=lambda item: (item[1][0], item[1][1]))
    return [(hearing_date, SITE + path) for path, (hearing_date, _) in ordered]


def pdf_text(data: bytes) -> str:
    """Extract text keeping the page layout, so the left margin notes stay separate from the description."""
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text(extraction_mode="layout") for page in reader.pages)


def parse_docket(text: str) -> list[dict]:
    """Split docket text into cases.

    Each case looks like this, with notes in the left column and the description on the right:

        Case No.         Application of Kraken Operating, LLC for an order amending the
        33085:           applicable orders for the Sheep Butte-Bakken Pool, McKenzie
        Notice to        County, ND, ...
        CONTINUE
    """
    cases = []
    section = "main"
    current = None
    column = 0  # where the description column starts for the current case

    def finish():
        if current and current["number"]:
            description = " ".join(" ".join(current["right"]).split())
            applicant = applicant_from(description)
            cases.append(
                {
                    "case_number": current["number"],
                    "section": current["section"],
                    "applicant": applicant,
                    "description": description,
                    "notes": " ".join(" ".join(current["left"]).split()) or None,
                }
            )

    for line in text.splitlines():
        if FURNITURE_RE.search(line):
            continue
        if line.strip().rstrip(":").lower() == "administrative docket":
            finish()
            current, section = None, "administrative"
            continue

        if m := CASE_LINE_RE.match(line):
            finish()
            column = len(m.group(1))
            current = {"number": None, "section": section, "left": [], "right": [m.group(2)]}
            continue
        if current is None:
            continue

        if not current["number"] and (m := NUMBER_LINE_RE.match(line)):
            current["number"] = m.group(2)
            column = max(column, len(m.group(1))) if m.group(3) else column
            current["right"].append(m.group(3))
            continue

        current["left"].append(line[:column].strip())
        current["right"].append(line[column:].strip())

    finish()
    return cases


def applicant_from(description: str) -> str | None:
    """The company that filed the application, or None for hearings called on a motion of the Commission."""
    m = APPLICANT_RE.match(description)
    return m.group(1).strip() if m else None


def reparse_applicants(conn) -> int:
    """Re-read applicant names from saved descriptions, after changing APPLICANT_RE. Returns how many changed."""
    changed = 0
    with conn:
        for row in conn.execute("SELECT rowid, description, applicant FROM docket_cases").fetchall():
            applicant = applicant_from(row["description"])
            if applicant != row["applicant"]:
                conn.execute("UPDATE docket_cases SET applicant = ? WHERE rowid = ?", (applicant, row["rowid"]))
                changed += 1
    return changed


def save_docket(conn, hearing_date: str, url: str, cases: list[dict]) -> int:
    """Add a docket's cases after any already saved for that date. Returns how many were new."""
    position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM docket_cases WHERE hearing_date = ?", (hearing_date,)
    ).fetchone()[0]
    added = 0
    for case in cases:
        # A supplement can repeat a case from the main docket; keep the first listing.
        exists = conn.execute(
            "SELECT 1 FROM docket_cases WHERE hearing_date = ? AND case_number = ?", (hearing_date, case["case_number"])
        ).fetchone()
        if exists:
            continue
        position += 1
        conn.execute(
            """INSERT INTO docket_cases (hearing_date, case_number, position, section, applicant, description, notes, url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                hearing_date,
                case["case_number"],
                position,
                case["section"],
                case["applicant"],
                case["description"],
                case["notes"],
                url,
            ),
        )
        added += 1
    conn.execute(
        "INSERT OR REPLACE INTO dockets (url, hearing_date, fetched_at) VALUES (?, ?, ?)",
        (url, hearing_date, datetime.now(UTC).isoformat(timespec="seconds")),
    )
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int, help="only this year")
    parser.add_argument("--refresh", action="store_true", help="download dockets again even if already saved")
    parser.add_argument("--reparse", action="store_true", help="only re-read applicant names from saved descriptions")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds to wait between downloads (default 1)")
    args = parser.parse_args()

    conn = db.connect()
    if args.reparse:
        print(f"Updated {reparse_applicants(conn)} applicant names.")
        return

    if args.year:
        years = [args.year]
    else:
        first = conn.execute("SELECT MIN(hearing_date) FROM videos").fetchone()[0]
        years = list(range(int(first[:4]), datetime.now().year + 1))

    for year in years:
        dockets = list_dockets(year)
        saved = {row[0] for row in conn.execute("SELECT url FROM dockets")}
        todo = dockets if args.refresh else [d for d in dockets if d[1] not in saved]
        print(f"{year}: {len(dockets)} docket PDFs, {len(todo)} to download")

        refreshed_dates = set()
        for hearing_date, url in todo:
            try:
                cases = parse_docket(pdf_text(fetch(url)))
            except Exception as e:
                print(f"  {url} -> error: {e}")
                continue
            with conn:
                # On a refresh, clear the date once so supplements are re-added in order.
                if args.refresh and hearing_date not in refreshed_dates:
                    conn.execute("DELETE FROM docket_cases WHERE hearing_date = ?", (hearing_date,))
                    refreshed_dates.add(hearing_date)
                added = save_docket(conn, hearing_date, url, cases)
            print(f"  {hearing_date} {url.rsplit('/', 1)[1]}: {len(cases)} cases ({added} new)")
            time.sleep(args.delay)

    conn.close()


if __name__ == "__main__":
    main()
