"""Download captions for every ND DMR hearing and save them to hearings.db.

Usage:
    python -m dmr_hearings.pipeline.scrape              # scrape every hearing not already done
    python -m dmr_hearings.pipeline.scrape --limit 3    # only scrape 3 hearings
    python -m dmr_hearings.pipeline.scrape --retry      # also retry hearings that failed or had no captions
"""

import argparse
import json
import re
import time
from datetime import UTC, datetime

import yt_dlp

from .. import db

STREAMS_URL = "https://www.youtube.com/@NDDMR/streams"


class Blocked(Exception):
    """YouTube is refusing requests. Stop and try again later."""


def list_hearings() -> list[dict]:
    """Return every hearing video on the channel's Live tab, newest first."""
    opts = {"extract_flat": True, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(STREAMS_URL, download=False)

    hearings = []
    for entry in info["entries"]:
        title = entry.get("title") or ""
        if "hearing" not in title.lower():
            continue
        # Skip streams that are live right now or scheduled for later.
        if entry.get("live_status") in ("is_live", "is_upcoming"):
            continue
        hearings.append(
            {
                "video_id": entry["id"],
                "title": title,
                "hearing_date": parse_date(title),
                "duration": int(entry["duration"]) if entry.get("duration") else None,
            }
        )
    return hearings


def parse_date(title: str) -> str | None:
    """Pull the hearing date out of titles like '8/27/2026 Hearings' or 'Dec 16, 2021 Hearings'."""
    title = re.sub(r"\s+", " ", title)
    if m := re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", title):
        month, day, year = map(int, m.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    if m := re.search(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", title):
        try:
            return datetime.strptime(" ".join(m.groups()), "%b %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def fetch_captions(video_id: str) -> list[tuple[int, int, str]] | None:
    """Download a video's English captions. Returns (start_ms, dur_ms, text) rows, or None if there are none."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignore_no_formats_error": True,  # we only need captions, not playable video formats
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            if "confirm you're not a bot" in str(e) or "HTTP Error 429" in str(e):
                raise Blocked(str(e)) from e
            raise

        # Prefer captions uploaded by the channel, then YouTube's auto-generated ones.
        tracks = (
            info.get("subtitles", {}).get("en")
            or info.get("automatic_captions", {}).get("en-orig")
            or info.get("automatic_captions", {}).get("en")
        )
        if not tracks:
            return None
        json3 = next((t for t in tracks if t.get("ext") == "json3"), None)
        if not json3:
            return None

        raw = ydl.urlopen(json3["url"]).read()

    return parse_json3(json.loads(raw))


def parse_json3(data: dict) -> list[tuple[int, int, str]]:
    """Turn YouTube's json3 caption format into (start_ms, dur_ms, text) rows."""
    rows = []
    for event in data.get("events", []):
        if "segs" not in event:
            continue
        text = "".join(seg.get("utf8", "") for seg in event["segs"])
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue  # auto-captions include events that are only line breaks
        rows.append((event["tStartMs"], event.get("dDurationMs", 0), text))
    return rows


def save_video(conn, video: dict) -> None:
    """Add a video to the database, or refresh its title and duration if it is already there."""
    conn.execute(
        """
        INSERT INTO videos (video_id, title, hearing_date, duration)
        VALUES (:video_id, :title, :hearing_date, :duration)
        ON CONFLICT(video_id) DO UPDATE SET
            title = excluded.title,
            hearing_date = excluded.hearing_date,
            duration = COALESCE(excluded.duration, videos.duration)
        """,
        video,
    )


def set_status(conn, video_id: str, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE videos SET status = ?, error = ?, scraped_at = ? WHERE video_id = ?",
        (status, error, datetime.now(UTC).isoformat(timespec="seconds"), video_id),
    )


def scrape(limit: int | None, delay: float, retry: bool) -> None:
    conn = db.connect()

    print("Listing hearings on the channel...")
    hearings = list_hearings()
    with conn:
        for video in hearings:
            save_video(conn, video)
    print(f"Found {len(hearings)} hearings.")

    statuses = ("pending", "no_captions", "error") if retry else ("pending",)
    todo = conn.execute(
        f"SELECT video_id, title FROM videos WHERE status IN ({','.join('?' * len(statuses))})"
        " ORDER BY hearing_date DESC",
        statuses,
    ).fetchall()
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} to scrape.\n")

    for i, row in enumerate(todo, 1):
        video_id, title = row["video_id"], row["title"]
        print(f"[{i}/{len(todo)}] {title} ({video_id}) ", end="", flush=True)

        try:
            captions = fetch_captions(video_id)
        except Blocked as e:
            print(f"\nYouTube is blocking requests. Stopping; run again later.\n{e}")
            break
        except Exception as e:
            with conn:
                set_status(conn, video_id, "error", str(e))
            print(f"-> error: {e}")
            continue

        # Replace any old captions and update the status in one transaction,
        # so a crash never leaves a video half-saved.
        with conn:
            conn.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
            if captions:
                conn.executemany(
                    "INSERT INTO segments (video_id, start_ms, dur_ms, text) VALUES (?, ?, ?, ?)",
                    [(video_id, *c) for c in captions],
                )
                set_status(conn, video_id, "done")
                print(f"-> {len(captions)} caption lines")
            else:
                set_status(conn, video_id, "no_captions")
                print("-> no captions yet")

        if i < len(todo):
            time.sleep(delay)

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="only scrape this many hearings")
    parser.add_argument("--delay", type=float, default=3.0, help="seconds to wait between videos (default 3)")
    parser.add_argument("--retry", action="store_true", help="retry hearings that failed or had no captions")
    args = parser.parse_args()
    scrape(args.limit, args.delay, args.retry)


if __name__ == "__main__":
    main()
