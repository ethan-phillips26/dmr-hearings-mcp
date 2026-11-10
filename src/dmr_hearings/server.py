"""MCP server exposing the hearing transcripts and dockets as read-only tools over stdio.

MCP clients start this themselves; to try it by hand:
    dmr-mcp                                        # speaks MCP on stdin/stdout
    npx @modelcontextprotocol/inspector dmr-mcp    # MCP Inspector
"""

import re
import threading
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import db, search

INSTRUCTIONS = """\
Search tools for North Dakota Industrial Commission oil and gas hearings (ND Department of
Mineral Resources), from the YouTube recordings of every hearing since December 2021 and the
official hearing dockets.

How the data works:
- Hearings are split into cases. Each case has a 5-digit case number (e.g. 33087), an
  applicant, and a docket description of what was requested (spacing units, pooling, well
  density, flaring exceptions, etc.).
- Transcripts come from YouTube auto-captions. Names, companies and numbers are sometimes
  misheard ("Merk's petroleum" for Murex Petroleum). Trust docket applicants and case numbers
  over the transcript's spelling.
- Case spans marked low confidence may start or end a little off.

How to answer:
- For topic questions, call search_transcripts, try a few different wordings, then use
  read_transcript to read around the best hits before drawing conclusions.
- For a company, pool or county, search_dockets is often faster than transcripts.
- For a specific case, call get_case.
- Always give the case number, hearing date, and the timestamp link for every claim so the
  user can check the recording. Don't state an outcome (approved, denied) unless the
  transcript says it; hearings usually end without a decision.
"""

server = MCPServer("dmr-hearings", instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

DateArg = Annotated[str | None, Field(description="YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$")]
CaseArg = Annotated[str, Field(description="5-digit case number, e.g. 33087", pattern=r"^\d{5}$")]


def connect():
    """A connection per call: tool calls can run on different threads, and SQLite connections can't be shared."""
    return db.connect()


def short(text: str | None, limit: int = 300) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


@server.tool(annotations=READ_ONLY)
def search_transcripts(
    query: Annotated[str, Field(description="What to look for, in plain words. Matches by keyword and by meaning.")],
    date_from: DateArg = None,
    date_to: DateArg = None,
    case_number: Annotated[str | None, Field(description="Only search within this case", pattern=r"^\d{5}$")] = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
) -> list[dict]:
    """Search hearing transcripts. Returns ~75-second excerpts with the hearing date, case, and a link to that moment in the video."""
    conn = connect()
    try:
        results = search.hybrid_search(conn, query, limit, date_from, date_to, case_number)
    finally:
        conn.close()
    return [
        {
            "hearing_date": r["hearing_date"],
            "hearing": r["title"],
            "video_id": r["video_id"],
            "timestamp": r["timestamp"],
            "url": r["url"],
            "cases": [
                {"case_number": c["case_number"], "applicant": c["applicant"], "request": short(c["description"], 200)}
                for c in r["cases"]
            ],
            "excerpt": r["text"],
        }
        for r in results
    ]


@server.tool(annotations=READ_ONLY)
def get_case(case_number: CaseArg) -> dict:
    """Everything about one case: docket listings (applicant, full request, notes) and every hearing where it was heard, continued or dismissed, with video links."""
    conn = connect()
    try:
        result = search.get_case(conn, case_number)
    finally:
        conn.close()
    if not result["dockets"] and not result["hearings"]:
        return {"case_number": case_number, "error": "No docket listing or hearing found for this case number."}
    for hearing in result["hearings"]:
        hearing.pop("start_ms", None)
        hearing.pop("end_ms", None)
    return result


@server.tool(annotations=READ_ONLY)
def search_dockets(
    query: Annotated[
        str, Field(description="Company, pool, county, formation, or type of request, e.g. 'Kraken McKenzie pooling'")
    ],
    date_from: DateArg = None,
    date_to: DateArg = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> list[dict]:
    """Search the official hearing dockets. Returns case numbers with applicant, request, docket date, and whether the case was heard on video."""
    conn = connect()
    try:
        rows = search.search_dockets(conn, query, limit, date_from, date_to)
    finally:
        conn.close()
    return [
        {
            "docket_date": r["hearing_date"],
            "case_number": r["case_number"],
            "applicant": r["applicant"],
            "request": short(r["description"], 400),
            "notes": r["notes"],
            "section": r["section"],
            "heard_on_video": bool(r["heard"]),
        }
        for r in rows
    ]


@server.tool(annotations=READ_ONLY)
def read_transcript(
    video_id: Annotated[str, Field(description="video_id from a search result")],
    timestamp: Annotated[
        str, Field(description="Where to read, as h:mm:ss or m:ss", pattern=r"^\d{1,2}(:\d{2}){0,2}$")
    ],
    minutes_before: Annotated[int, Field(ge=0, le=10)] = 1,
    minutes_after: Annotated[int, Field(ge=1, le=15)] = 3,
) -> dict:
    """Read the transcript around a moment in a hearing, to get more context than a search excerpt."""
    conn = connect()
    try:
        result = search.read_transcript(
            conn, video_id, search.parse_time(timestamp), minutes_before * 60_000, minutes_after * 60_000
        )
    finally:
        conn.close()
    if "error" in result:
        return result
    # One line per caption is wasteful; group them into paragraphs of about 20 seconds.
    paragraphs, current, started = [], [], None
    for line in result["lines"]:
        if started is None:
            started = line
        current.append(line["text"])
        if line["start_ms"] - started["start_ms"] >= 20_000:
            paragraphs.append(f"[{started['timestamp']}] {' '.join(current)}")
            current, started = [], None
    if current:
        paragraphs.append(f"[{started['timestamp']}] {' '.join(current)}")
    return {
        "hearing_date": result["hearing_date"],
        "hearing": result["title"],
        "url": result["url"],
        "cases": [{"case_number": c["case_number"], "applicant": c["applicant"]} for c in result["cases"]],
        "transcript": "\n".join(re.sub(r"\s+", " ", p) for p in paragraphs),
    }


@server.tool(annotations=READ_ONLY)
def list_hearings(date_from: DateArg = None, date_to: DateArg = None) -> list[dict]:
    """List recorded hearings with their date, length, number of cases heard, and video link."""
    conn = connect()
    try:
        rows = search.list_hearings(conn, date_from, date_to)
    finally:
        conn.close()
    return [
        {
            "hearing_date": r["hearing_date"],
            "hearing": r["title"],
            "video_id": r["video_id"],
            "hours": round((r["duration"] or 0) / 3600, 1),
            "cases_heard": r["cases_heard"],
            "url": r["url"],
        }
        for r in rows
        if r["status"] == "done"
    ]


def main() -> None:
    # Load the embedding model in the background so the first search doesn't wait ~10 seconds for it.
    threading.Thread(target=search.model, daemon=True).start()
    server.run("stdio")


if __name__ == "__main__":
    main()
