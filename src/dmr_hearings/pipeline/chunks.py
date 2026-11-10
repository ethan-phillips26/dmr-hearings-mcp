"""Group each hearing's caption lines into ~75 second chunks tagged with their case.

Usage:
    python -m dmr_hearings.pipeline.chunks                        # build chunks for hearings that are new or changed
    python -m dmr_hearings.pipeline.chunks --rebuild              # rebuild every hearing's chunks
    python -m dmr_hearings.pipeline.chunks --video m2-k83Vx4XE -v # one hearing, printing its chunks
"""

import argparse
import hashlib
import re

from .. import db
from ..util import fmt

CHUNK_MS = 75_000  # length of each chunk
STEP_MS = 60_000  # a new chunk starts every 60 seconds, so neighbours overlap by 15
MIN_TAIL_MS = 20_000  # a leftover shorter than this is added to the chunk before it

TAG_RE = re.compile(r"\[[^\]]*\]")  # [Applause], [Music]


def load_segments(conn, video_id: str) -> list[tuple[int, int, str]]:
    rows = conn.execute(
        "SELECT start_ms, dur_ms, text FROM segments WHERE video_id = ? ORDER BY start_ms", (video_id,)
    ).fetchall()
    segments = []
    for row in rows:
        text = " ".join(TAG_RE.sub(" ", row["text"]).split())
        if text:
            segments.append((row["start_ms"], row["start_ms"] + row["dur_ms"], text))
    return segments


def load_spans(conn, video_id: str) -> list[tuple[int, int, str]]:
    return [
        (row["start_ms"], row["end_ms"], row["case_number"])
        for row in conn.execute(
            "SELECT start_ms, end_ms, case_number FROM cases WHERE video_id = ? AND status = 'heard' ORDER BY start_ms",
            (video_id,),
        )
    ]


def fingerprint(segments: list, spans: list) -> str:
    return hashlib.sha256(repr((segments, spans)).encode()).hexdigest()


def cases_at(ms: int, spans: list[tuple[int, int, str]]) -> frozenset[str]:
    """The case(s) being heard at a moment in the video. Usually one, sometimes none."""
    return frozenset(number for start, end, number in spans if start <= ms <= end)


def make_chunks(segments: list[tuple[int, int, str]], spans: list[tuple[int, int, str]]) -> list[dict]:
    """Split a hearing into chunks.

    1. Group consecutive caption lines that belong to the same case(s) into runs,
       so no chunk ever mixes the end of one case with the start of the next.
    2. Cut each run into overlapping 75 second windows.
    """
    runs: list[tuple[frozenset[str], list]] = []
    for segment in segments:
        cases = cases_at(segment[0], spans)
        if runs and runs[-1][0] == cases:
            runs[-1][1].append(segment)
        else:
            runs.append((cases, [segment]))

    chunks = []
    for cases, run in runs:
        windows: list[list] = []
        window_start = run[0][0]
        while True:
            window = [s for s in run if window_start <= s[0] < window_start + CHUNK_MS]
            if window:
                windows.append(window)
            if window_start + CHUNK_MS > run[-1][0]:
                break
            window_start += STEP_MS

        # A short leftover at the end of a run is merged into the previous window.
        if len(windows) > 1 and windows[-1][-1][1] - windows[-1][0][0] < MIN_TAIL_MS:
            tail = windows.pop()
            windows[-1] += [s for s in tail if s not in windows[-1]]

        for window in windows:
            chunks.append(
                {
                    "start_ms": window[0][0],
                    "end_ms": max(s[1] for s in window),
                    "text": " ".join(s[2] for s in window),
                    "cases": sorted(cases),
                }
            )
    return chunks


def build_video(conn, video_id: str, rebuild: bool) -> list[dict] | None:
    """Rebuild one hearing's chunks if its captions or case spans changed. Returns the chunks, or None if skipped."""
    segments = load_segments(conn, video_id)
    spans = load_spans(conn, video_id)
    fp = fingerprint(segments, spans)

    saved = conn.execute("SELECT fingerprint FROM chunk_builds WHERE video_id = ?", (video_id,)).fetchone()
    if saved and saved["fingerprint"] == fp and not rebuild:
        return None

    chunks = make_chunks(segments, spans)
    with conn:
        conn.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))  # chunk_cases rows go with them
        for chunk in chunks:
            chunk_id = conn.execute(
                "INSERT INTO chunks (video_id, start_ms, end_ms, text) VALUES (?, ?, ?, ?)",
                (video_id, chunk["start_ms"], chunk["end_ms"], chunk["text"]),
            ).lastrowid
            conn.executemany(
                "INSERT INTO chunk_cases (chunk_id, case_number) VALUES (?, ?)",
                [(chunk_id, number) for number in chunk["cases"]],
            )
        conn.execute("INSERT OR REPLACE INTO chunk_builds (video_id, fingerprint) VALUES (?, ?)", (video_id, fp))
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--video", help="only this video ID")
    parser.add_argument("--rebuild", action="store_true", help="rebuild even if nothing changed")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each chunk")
    args = parser.parse_args()

    conn = db.connect()
    query = "SELECT video_id, title FROM videos WHERE status = 'done'"
    params: tuple = ()
    if args.video:
        query += " AND video_id = ?"
        params = (args.video,)
    videos = conn.execute(query + " ORDER BY hearing_date DESC", params).fetchall()

    built = skipped = 0
    for video in videos:
        chunks = build_video(conn, video["video_id"], args.rebuild or bool(args.video))
        if chunks is None:
            skipped += 1
            continue
        built += 1
        tagged = sum(1 for c in chunks if c["cases"])
        print(f"{video['title']} ({video['video_id']}): {len(chunks)} chunks, {tagged} tagged with a case")
        if args.verbose:
            for c in chunks:
                cases = ",".join(c["cases"]) or "-"
                print(f"    {fmt(c['start_ms'])}-{fmt(c['end_ms'])}  {cases:<12} {c['text'][:90]}")

    total, words, tagged = conn.execute(
        """SELECT COUNT(*), AVG(LENGTH(text) - LENGTH(REPLACE(text, ' ', '')) + 1),
                  (SELECT COUNT(DISTINCT chunk_id) FROM chunk_cases)
           FROM chunks"""
    ).fetchone()
    print(
        f"\nRebuilt {built} hearings, {skipped} unchanged. "
        f"Database has {total} chunks, avg {words or 0:.0f} words, {tagged} tagged with a case."
    )
    conn.close()


if __name__ == "__main__":
    main()
