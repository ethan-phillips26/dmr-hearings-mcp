"""Build the keyword and embedding indexes that search uses.

Usage:
    python -m dmr_hearings.pipeline.embed                # rebuild keyword indexes, embed chunks that don't have an embedding yet
    python -m dmr_hearings.pipeline.embed --limit 500    # only embed 500 chunks (for testing)
    python -m dmr_hearings.pipeline.embed --reembed      # throw away all embeddings and embed every chunk again

Embedding is resumable: progress is saved after every batch, so stopping and
running again continues where it left off.
"""

import argparse
import time

import numpy as np
from fastembed import TextEmbedding

from .. import config, db

BATCH_SIZE = 64


def rebuild_keyword_indexes(conn) -> None:
    """Re-read all chunk and docket text into the FTS5 indexes. Takes a few seconds."""
    with conn:
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        conn.execute("INSERT INTO docket_fts(docket_fts) VALUES ('rebuild')")


def remove_stale_embeddings(conn) -> int:
    """Delete embeddings whose chunk was removed when chunks.py rebuilt a hearing."""
    embedded = {row[0] for row in conn.execute("SELECT chunk_id FROM chunks_vec")}
    current = {row[0] for row in conn.execute("SELECT chunk_id FROM chunks")}
    stale = embedded - current
    with conn:
        conn.executemany("DELETE FROM chunks_vec WHERE chunk_id = ?", [(i,) for i in stale])
    return len(stale)


def embedding_text(chunk, cases: list[str], applicants: dict[str, str]) -> str:
    """The text that gets embedded: a short header with the date and case, then the transcript.

    The header lets a search for a company or case match chunks that never say it,
    since most of a case's testimony doesn't repeat the applicant's name.
    """
    header = f"{chunk['hearing_date']} hearing."
    if cases:
        labels = [f"{n} ({applicants[n]})" if n in applicants else n for n in cases]
        header += f" Case {', '.join(labels)}."
    return f"{header}\n{chunk['text']}"


def embed_chunks(conn, limit: int | None) -> None:
    embedded = {row[0] for row in conn.execute("SELECT chunk_id FROM chunks_vec")}
    todo = [row[0] for row in conn.execute("SELECT chunk_id FROM chunks ORDER BY chunk_id") if row[0] not in embedded]
    if limit:
        todo = todo[:limit]
    if not todo:
        print("All chunks already have embeddings.")
        return

    applicants = {
        row["case_number"]: row["applicant"]
        for row in conn.execute("SELECT case_number, applicant FROM docket_cases WHERE applicant IS NOT NULL")
    }

    print(f"Loading {db.EMBED_MODEL} (downloads to {config.model_dir()} the first time)...")
    model = TextEmbedding(db.EMBED_MODEL, cache_dir=str(config.model_dir()))

    print(f"Embedding {len(todo)} chunks...")
    started = time.time()
    done = 0
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i : i + BATCH_SIZE]
        marks = ",".join("?" * len(batch))
        chunks = conn.execute(
            f"""SELECT c.chunk_id, c.text, v.hearing_date FROM chunks c JOIN videos v USING (video_id)
                WHERE c.chunk_id IN ({marks}) ORDER BY c.chunk_id""",
            batch,
        ).fetchall()
        cases: dict[int, list[str]] = {}
        for row in conn.execute(
            f"SELECT chunk_id, case_number FROM chunk_cases WHERE chunk_id IN ({marks}) ORDER BY case_number", batch
        ):
            cases.setdefault(row["chunk_id"], []).append(row["case_number"])

        texts = [embedding_text(c, cases.get(c["chunk_id"], []), applicants) for c in chunks]
        vectors = model.embed(texts, batch_size=BATCH_SIZE)

        with conn:
            conn.executemany(
                "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                [
                    (c["chunk_id"], np.asarray(v, dtype=np.float32).tobytes())
                    for c, v in zip(chunks, vectors, strict=False)
                ],
            )

        done += len(chunks)
        rate = done / (time.time() - started)
        remaining = (len(todo) - done) / rate
        print(f"  {done}/{len(todo)}  {rate:.1f} chunks/s  about {remaining / 60:.0f} min left", flush=True)

    print(f"Done in {(time.time() - started) / 60:.1f} min.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="only embed this many chunks")
    parser.add_argument("--reembed", action="store_true", help="delete all embeddings and start over")
    args = parser.parse_args()

    conn = db.connect()

    print("Rebuilding keyword indexes...")
    rebuild_keyword_indexes(conn)

    if args.reembed:
        with conn:
            conn.execute("DELETE FROM chunks_vec")
    removed = remove_stale_embeddings(conn)
    if removed:
        print(f"Removed {removed} embeddings for chunks that no longer exist.")

    embed_chunks(conn, args.limit)

    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    embedded = conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
    print(f"{embedded}/{total} chunks have embeddings.")
    conn.close()


if __name__ == "__main__":
    main()
