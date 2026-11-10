"""Hybrid (keyword + semantic) search over hearing transcripts and dockets, plus a small CLI.

Usage:
    dmr-search "neighbors upset about noise from the rig"
    dmr-search "flaring exception" --from 2025-01-01 --limit 5
    dmr-search --case 33087
    dmr-search --dockets "Kraken McKenzie"
    dmr-search --read m2-k83Vx4XE 0:28:19
"""

import argparse
import re

import numpy as np

from . import config, db
from .util import fmt, parse_time, video_url

RRF_K = 60  # reciprocal rank fusion constant; larger values flatten the difference between ranks
CANDIDATES = 50  # how many results each search contributes before merging
NEARBY_MS = 60_000  # results from the same video closer than this are treated as the same moment

STOPWORDS = set(
    """
a an and are as at be been but by can could did do does for from had has have how i if in into is it its
me my of on or our so than that the their them then there these they this to was we were what when where
which who why will with would you your any about there has been case cases hearing hearings
""".split()
)

_model = None


def model():
    """Load the embedding model the first time it's needed (about 10 seconds), then reuse it."""
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(db.EMBED_MODEL, cache_dir=str(config.model_dir()))
    return _model


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query.

    FTS5 treats punctuation, AND/OR/NOT and unbalanced quotes as syntax, so each word is
    quoted on its own. Words are joined with OR and BM25 ranking puts chunks that contain
    more of them, and rarer ones, first. Common words are dropped so they don't match everything.
    """
    words = [w for w in re.findall(r"[A-Za-z0-9]+", text.lower()) if w not in STOPWORDS]
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def _filters(date_from=None, date_to=None, case_number=None, video_id=None) -> tuple[str, list]:
    """SQL conditions on chunks c / videos v for the optional filters."""
    sql, params = [], []
    if date_from:
        sql.append("v.hearing_date >= ?")
        params.append(date_from)
    if date_to:
        sql.append("v.hearing_date <= ?")
        params.append(date_to)
    if case_number:
        sql.append("EXISTS (SELECT 1 FROM chunk_cases cc WHERE cc.chunk_id = c.chunk_id AND cc.case_number = ?)")
        params.append(case_number)
    if video_id:
        sql.append("c.video_id = ?")
        params.append(video_id)
    return "".join(f" AND {s}" for s in sql), params


def keyword_search(conn, query: str, limit: int, **filters) -> list[int]:
    """Chunk IDs ranked by FTS5 (BM25), best first."""
    match = fts_query(query)
    if not match:
        return []
    where, params = _filters(**filters)
    rows = conn.execute(
        f"""SELECT c.chunk_id FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.rowid
            JOIN videos v USING (video_id)
            WHERE chunks_fts MATCH ? {where}
            ORDER BY chunks_fts.rank LIMIT ?""",
        [match, *params, limit],
    )
    return [row[0] for row in rows]


def meaning_search(conn, query: str, limit: int, **filters) -> list[tuple[int, float]]:
    """(chunk ID, cosine distance) ranked by embedding similarity, best first."""
    vector = np.asarray(next(model().query_embed([query])), dtype=np.float32).tobytes()
    where, params = _filters(**filters)
    # vec0 finds the nearest k before the filters are applied, so ask for more when filtering.
    k = limit * 40 if where else limit
    rows = conn.execute(
        f"""SELECT m.chunk_id, m.distance
            FROM (SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ?) m
            JOIN chunks c ON c.chunk_id = m.chunk_id
            JOIN videos v USING (video_id)
            WHERE 1 = 1 {where}
            ORDER BY m.distance LIMIT ?""",
        [vector, min(k, 4096), *params, limit],
    )
    return [(row[0], row[1]) for row in rows]


def hybrid_search(
    conn,
    query: str,
    limit: int = 10,
    date_from: str | None = None,
    date_to: str | None = None,
    case_number: str | None = None,
    video_id: str | None = None,
) -> list[dict]:
    """Search transcripts by keyword and by meaning, merge the two rankings, and return the best chunks."""
    filters = dict(date_from=date_from, date_to=date_to, case_number=case_number, video_id=video_id)
    keyword = keyword_search(conn, query, CANDIDATES, **filters)
    meaning = meaning_search(conn, query, CANDIDATES, **filters)

    # Reciprocal rank fusion: each list adds 1 / (60 + rank). Found by both beats found by one.
    scores: dict[int, dict] = {}
    for rank, chunk_id in enumerate(keyword, 1):
        entry = scores.setdefault(
            chunk_id, {"score": 0.0, "keyword_rank": None, "meaning_rank": None, "distance": None}
        )
        entry["score"] += 1 / (RRF_K + rank)
        entry["keyword_rank"] = rank
    for rank, (chunk_id, distance) in enumerate(meaning, 1):
        entry = scores.setdefault(
            chunk_id, {"score": 0.0, "keyword_rank": None, "meaning_rank": None, "distance": None}
        )
        entry["score"] += 1 / (RRF_K + rank)
        entry["meaning_rank"] = rank
        entry["distance"] = round(distance, 3)

    ranked = sorted(scores.items(), key=lambda item: item[1]["score"], reverse=True)

    results: list[dict] = []
    for chunk_id, entry in ranked:
        result = chunk_details(conn, chunk_id)
        # Neighbouring chunks overlap, so two hits a few seconds apart are the same moment.
        if any(
            r["video_id"] == result["video_id"] and abs(r["start_ms"] - result["start_ms"]) < NEARBY_MS for r in results
        ):
            continue
        result.update(
            score=round(entry["score"], 5),
            keyword_rank=entry["keyword_rank"],
            meaning_rank=entry["meaning_rank"],
            distance=entry["distance"],
        )
        results.append(result)
        if len(results) == limit:
            break
    return results


def chunk_details(conn, chunk_id: int) -> dict:
    row = conn.execute(
        """SELECT c.chunk_id, c.video_id, c.start_ms, c.end_ms, c.text, v.title, v.hearing_date
           FROM chunks c JOIN videos v USING (video_id) WHERE c.chunk_id = ?""",
        (chunk_id,),
    ).fetchone()
    numbers = [
        r[0]
        for r in conn.execute(
            "SELECT case_number FROM chunk_cases WHERE chunk_id = ? ORDER BY case_number", (chunk_id,)
        )
    ]
    return {
        "chunk_id": row["chunk_id"],
        "video_id": row["video_id"],
        "title": row["title"],
        "hearing_date": row["hearing_date"],
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "timestamp": fmt(row["start_ms"]),
        "url": video_url(row["video_id"], row["start_ms"]),
        "text": row["text"],
        "cases": [docket_summary(conn, n, row["hearing_date"]) for n in numbers],
    }


def docket_summary(conn, case_number: str, near_date: str | None = None) -> dict:
    """A case's applicant and description from the docket listing closest to near_date."""
    row = conn.execute(
        """SELECT applicant, description FROM docket_cases WHERE case_number = ?
           ORDER BY ABS(julianday(hearing_date) - julianday(COALESCE(?, hearing_date))) LIMIT 1""",
        (case_number, near_date),
    ).fetchone()
    return {
        "case_number": case_number,
        "applicant": row["applicant"] if row else None,
        "description": row["description"] if row else None,
    }


def get_case(conn, case_number: str) -> dict:
    """Everything known about a case: docket listings and every hearing where it came up."""
    dockets = [
        dict(r)
        for r in conn.execute(
            """SELECT hearing_date, section, applicant, description, notes, url AS docket_url
           FROM docket_cases WHERE case_number = ? ORDER BY hearing_date""",
            (case_number,),
        )
    ]
    hearings = []
    for r in conn.execute(
        """SELECT c.video_id, v.title, v.hearing_date, c.status, c.start_ms, c.end_ms, c.confidence
           FROM cases c JOIN videos v USING (video_id) WHERE c.case_number = ?
           ORDER BY v.hearing_date, c.start_ms""",
        (case_number,),
    ):
        hearing = dict(r)
        if r["start_ms"] is not None:
            hearing["start"] = fmt(r["start_ms"])
            hearing["end"] = fmt(r["end_ms"])
            hearing["url"] = video_url(r["video_id"], r["start_ms"])
        hearings.append(hearing)
    return {"case_number": case_number, "dockets": dockets, "hearings": hearings}


def search_dockets(
    conn, query: str, limit: int = 20, date_from: str | None = None, date_to: str | None = None
) -> list[dict]:
    """Keyword search over docket entries: applicants, pools, counties, what was requested."""
    match = fts_query(query)
    if not match:
        return []
    where, params = [], []
    if date_from:
        where.append("d.hearing_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("d.hearing_date <= ?")
        params.append(date_to)
    rows = conn.execute(
        f"""SELECT d.hearing_date, d.case_number, d.section, d.applicant, d.description, d.notes,
                   EXISTS (SELECT 1 FROM cases c WHERE c.case_number = d.case_number AND c.status = 'heard') AS heard
            FROM docket_fts JOIN docket_cases d ON d.rowid = docket_fts.rowid
            WHERE docket_fts MATCH ? {"".join(f" AND {w}" for w in where)}
            ORDER BY docket_fts.rank LIMIT ?""",
        [match, *params, limit],
    )
    return [dict(r) for r in rows]


def read_transcript(conn, video_id: str, at_ms: int, before_ms: int = 60_000, after_ms: int = 180_000) -> dict:
    """Caption lines around a moment in a hearing, for reading more context than one chunk."""
    video = conn.execute("SELECT title, hearing_date FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if not video:
        return {"error": f"unknown video {video_id}"}
    lines = [
        {"timestamp": fmt(r["start_ms"]), "start_ms": r["start_ms"], "text": r["text"]}
        for r in conn.execute(
            "SELECT start_ms, text FROM segments WHERE video_id = ? AND start_ms BETWEEN ? AND ? ORDER BY start_ms",
            (video_id, max(at_ms - before_ms, 0), at_ms + after_ms),
        )
    ]
    # Every case heard during the lines returned, since the window can cross from one case into the next.
    numbers = [
        r[0]
        for r in conn.execute(
            """SELECT case_number FROM cases WHERE video_id = ? AND status = 'heard' AND start_ms <= ? AND end_ms >= ?
           ORDER BY start_ms""",
            (video_id, at_ms + after_ms, max(at_ms - before_ms, 0)),
        )
    ]
    return {
        "video_id": video_id,
        "title": video["title"],
        "hearing_date": video["hearing_date"],
        "url": video_url(video_id, at_ms),
        "cases": [docket_summary(conn, n, video["hearing_date"]) for n in numbers],
        "lines": lines,
    }


def list_hearings(conn, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT v.video_id, v.title, v.hearing_date, v.duration, v.status,
                  (SELECT COUNT(*) FROM cases c WHERE c.video_id = v.video_id AND c.status = 'heard') AS cases_heard
           FROM videos v
           WHERE (? IS NULL OR v.hearing_date >= ?) AND (? IS NULL OR v.hearing_date <= ?)
           ORDER BY v.hearing_date DESC, v.title""",
        (date_from, date_from, date_to, date_to),
    )
    return [dict(r) | {"url": video_url(r["video_id"], 0)} for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", nargs="*", help="what to search for")
    parser.add_argument("--from", dest="date_from", help="earliest hearing date, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="latest hearing date, YYYY-MM-DD")
    parser.add_argument("--case", help="with a query: only search this case. Alone: show everything about the case")
    parser.add_argument("--video", help="only search this video ID")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dockets", action="store_true", help="search docket entries instead of transcripts")
    parser.add_argument("--read", nargs=2, metavar=("VIDEO_ID", "TIME"), help="print the transcript around a time")
    args = parser.parse_args()
    query = " ".join(args.query)
    conn = db.connect()

    if args.read:
        result = read_transcript(conn, args.read[0], parse_time(args.read[1]))
        print(f"{result['hearing_date']}  {result['title']}  {result['url']}")
        for case in result["cases"]:
            print(f"Case {case['case_number']}: {case['applicant'] or ''}")
        for line in result["lines"]:
            print(f"  {line['timestamp']}  {line['text']}")

    elif args.case and not query:
        result = get_case(conn, args.case)
        print(f"Case {args.case}")
        for d in result["dockets"]:
            print(f"  Docket {d['hearing_date']} ({d['section']}){'  ' + d['notes'] if d['notes'] else ''}")
            print(f"    {d['description'][:300]}")
        for h in result["hearings"]:
            span = f"{h['start']} - {h['end']}  {h['url']}" if "url" in h else ""
            print(f"  {h['hearing_date']}  {h['status']:<10} {span}  ({h['confidence']} confidence)")

    elif args.dockets:
        for d in search_dockets(conn, query, args.limit, args.date_from, args.date_to):
            print(
                f"{d['hearing_date']}  {d['case_number']}  {'heard' if d['heard'] else 'not heard':<9}  {d['applicant'] or '-'}"
            )
            print(f"    {d['description'][:200]}\n")

    else:
        results = hybrid_search(conn, query, args.limit, args.date_from, args.date_to, args.case, args.video)
        embedded, total = conn.execute(
            "SELECT (SELECT COUNT(*) FROM chunks_vec), (SELECT COUNT(*) FROM chunks)"
        ).fetchone()
        if embedded < total:
            print(f"(meaning search covers {embedded}/{total} chunks; embedding isn't finished)\n")
        for r in results:
            cases = "; ".join(f"{c['case_number']} {c['applicant'] or ''}".strip() for c in r["cases"]) or "no case"
            ranks = f"keyword #{r['keyword_rank'] or '-'}, meaning #{r['meaning_rank'] or '-'}"
            print(f"{r['hearing_date']}  {r['timestamp']}  {cases}")
            print(f"  {r['url']}   ({ranks})")
            print(f"  {r['text'][:300]}\n")

    conn.close()


if __name__ == "__main__":
    main()
