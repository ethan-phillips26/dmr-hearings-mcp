"""Database schema and connection for hearings.db."""

import sqlite3
from pathlib import Path

import sqlite_vec

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id     TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    hearing_date TEXT,              -- YYYY-MM-DD, parsed from the title
    duration     INTEGER,           -- seconds
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending, done, no_captions, error
    error        TEXT,
    scraped_at   TEXT
);

-- One row per caption line, exactly as YouTube timed it.
CREATE TABLE IF NOT EXISTS segments (
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    start_ms INTEGER NOT NULL,
    dur_ms   INTEGER NOT NULL,
    text     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS segments_video ON segments(video_id, start_ms);

-- One row per case per hearing. A case continued at one hearing and heard at
-- a later one has a row for each.
CREATE TABLE IF NOT EXISTS cases (
    video_id    TEXT NOT NULL REFERENCES videos(video_id),
    case_number TEXT NOT NULL,
    status      TEXT NOT NULL,      -- heard, continued, dismissed, withdrawn
    start_ms    INTEGER,            -- NULL unless heard
    end_ms      INTEGER,            -- NULL unless heard
    confidence  TEXT NOT NULL       -- high, low
);

CREATE INDEX IF NOT EXISTS cases_number ON cases(case_number);
CREATE INDEX IF NOT EXISTS cases_video ON cases(video_id, start_ms);

-- Every time a case number is said. case_number is NULL when the captions
-- garbled it and it could not be repaired; raw keeps what the captions said.
CREATE TABLE IF NOT EXISTS case_mentions (
    video_id    TEXT NOT NULL REFERENCES videos(video_id),
    case_number TEXT,
    raw         TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- mention, status, conclude
    start_ms    INTEGER NOT NULL,
    context     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS case_mentions_number ON case_mentions(case_number);

-- Official docket PDFs (main docket and supplements) that have been downloaded.
CREATE TABLE IF NOT EXISTS dockets (
    url          TEXT PRIMARY KEY,
    hearing_date TEXT NOT NULL,     -- YYYY-MM-DD
    fetched_at   TEXT NOT NULL
);

-- One row per case listed on a hearing date's docket, in docket order.
CREATE TABLE IF NOT EXISTS docket_cases (
    hearing_date TEXT NOT NULL,
    case_number  TEXT NOT NULL,
    position     INTEGER NOT NULL,  -- order on the docket, main docket first, then supplements
    section      TEXT NOT NULL,     -- main, administrative
    applicant    TEXT,
    description  TEXT NOT NULL,
    notes        TEXT,              -- left margin notes, e.g. "Notice to CONTINUE to 09/2026"
    url          TEXT NOT NULL,
    PRIMARY KEY (hearing_date, case_number)
);

CREATE INDEX IF NOT EXISTS docket_cases_number ON docket_cases(case_number);

-- About 75 seconds of transcript each. These are what get searched and embedded.
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused, so an old embedding can't match new text
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER NOT NULL,
    text     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_video ON chunks(video_id, start_ms);

-- Which case(s) each chunk belongs to. Chunks outside any case (opening remarks,
-- the docket list, breaks) have no rows here.
CREATE TABLE IF NOT EXISTS chunk_cases (
    chunk_id    INTEGER NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    case_number TEXT NOT NULL,
    PRIMARY KEY (chunk_id, case_number)
);

CREATE INDEX IF NOT EXISTS chunk_cases_number ON chunk_cases(case_number);

-- A fingerprint of the captions and case spans each video's chunks were built from,
-- so chunks are only rebuilt (and re-embedded) when something actually changed.
CREATE TABLE IF NOT EXISTS chunk_builds (
    video_id    TEXT PRIMARY KEY REFERENCES videos(video_id),
    fingerprint TEXT NOT NULL
);

-- Keyword indexes. They read their text from chunks and docket_cases
-- (content=...) instead of storing a second copy, and are rebuilt by index.py.
-- porter stemming lets "flaring" match "flare" and "flared".
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='chunk_id', tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS docket_fts USING fts5(
    case_number, applicant, description, content='docket_cases', tokenize='porter unicode61'
);

-- One embedding per chunk, from index.py.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding float[{dim}] distance_metric=cosine
);
"""

EMBED_MODEL = "BAAI/bge-base-en-v1.5"
EMBED_DIM = 768


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path) if path else config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)  # adds the vec0 table type used for embeddings
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode = WAL")  # lets the MCP server read while the pipeline writes
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.replace("{dim}", str(EMBED_DIM)))
    return conn
