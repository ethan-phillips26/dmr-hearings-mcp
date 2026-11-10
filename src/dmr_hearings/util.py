"""Timestamp and link helpers shared by the search code and the pipeline."""


def fmt(ms: int | None) -> str:
    """Milliseconds -> 'h:mm:ss'. None (a case with no recorded span) -> '-'."""
    if ms is None:
        return "-"
    s = ms // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_time(text: str) -> int:
    """'1:12:40', '12:40' or '4360' (seconds) -> milliseconds."""
    seconds = 0
    for part in text.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds * 1000


def video_url(video_id: str, ms: int) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&t={ms // 1000}s"
