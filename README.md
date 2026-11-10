# dmr-hearings-mcp

An [MCP](https://modelcontextprotocol.io) server for searching North Dakota Industrial Commission oil and gas
hearings. It indexes the auto-captions of the hearing recordings on the
[NDDMR YouTube channel](https://www.youtube.com/@NDDMR/streams) together with the official hearing dockets, so an
MCP client such as Claude Desktop can answer questions like *"what did operators say about flaring exceptions in
2025?"* with case numbers and links to the exact moment in the video.

Everything runs locally: a SQLite database, a small CPU embedding model, no API keys.

## Tools

| Tool | Purpose |
|---|---|
| `search_transcripts` | Keyword + semantic search; returns ~75 second excerpts with case, date and a timestamped video link |
| `read_transcript` | Read the transcript around a timestamp, for more context than an excerpt |
| `get_case` | Docket entries and every hearing where a 5-digit case number was heard, continued or dismissed |
| `search_dockets` | Search docket entries by company, pool, county or type of request |
| `list_hearings` | Recorded hearings, filterable by date |

All tools are read-only.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/<you>/dmr-hearings-mcp
cd dmr-hearings-mcp
uv sync
```

## Build the data

The database (~200 MB) and embedding model (~220 MB) are too large for git, so you build them locally. They live in
`DMR_DATA_DIR`, which defaults to the per-user data directory (`~/.local/share/dmr-hearings` on Linux).

```
uv run dmr-update
```

This downloads captions and dockets, splits hearings into cases, chunks the transcripts and embeds them. The first
run covers the whole backlog and takes a while (embedding is the slow part and resumes if interrupted); later runs
only process new hearings. Use `--limit 3` to try it on a few hearings first.

## Connect a client

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "dmr-hearings": {
      "command": "uv",
      "args": ["--directory", "/path/to/dmr-hearings-mcp", "run", "dmr-mcp"],
      "env": { "DMR_DATA_DIR": "/path/to/data" }
    }
  }
}
```

Claude Code: `claude mcp add dmr-hearings -- uv --directory /path/to/dmr-hearings-mcp run dmr-mcp`

There is also a command line search for checking results without a client:

```
uv run dmr-search "neighbors upset about noise from the rig" --from 2025-01-01
uv run dmr-search --case 33087
```

## How it works

```
scrape ──> dockets ──> cases ──> chunks ──> embed ──> hearings.db
 yt-dlp     NDIC PDFs   split      75 s       bge-base    SQLite: FTS5 + sqlite-vec
 captions   via pypdf   by case    windows
```

- **scrape** lists the channel's hearing videos and downloads English captions with `yt-dlp` (no video or audio).
- **dockets** downloads the official docket PDFs and parses each listed case: number, applicant, request, margin notes.
- **cases** finds where each case starts and ends in a hearing. Hearings follow a script ("next case is case number
  33086" ... "that concludes the hearing for case number 33086"), so the spans come from those phrases. Case numbers
  the captions garble are repaired against the docket when there is exactly one plausible fix, and uncertain spans are
  marked `low` confidence.
- **chunks** cuts each case's transcript into overlapping 75 second windows that never cross a case boundary.
- **embed** builds the FTS5 keyword index and embeds each chunk with `BAAI/bge-base-en-v1.5` via
  [fastembed](https://github.com/qdrant/fastembed). Chunks are embedded with their case number and applicant so a
  search for a company finds testimony that never says its name.
- **search** runs keyword (BM25) and vector search, then merges the rankings with reciprocal rank fusion.

Modules are in `src/dmr_hearings/`; each pipeline step can be run on its own, e.g.
`uv run python -m dmr_hearings.pipeline.cases --video <id> -v`.

## Limitations

- Case boundaries are inferred from phrases in the captions, so a few spans start or end a little off. Low confidence
  cases are flagged.
- Outcomes are rarely stated in the recording; hearings usually end without a ruling, so the tools don't report one.
- If YouTube starts blocking requests, update yt-dlp: `uv lock --upgrade-package yt-dlp`.

## Development

```
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```
