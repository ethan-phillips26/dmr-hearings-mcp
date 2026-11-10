from dmr_hearings.util import fmt, parse_time, video_url


def test_fmt():
    assert fmt(0) == "0:00:00"
    assert fmt(4_360_000) == "1:12:40"
    assert fmt(None) == "-"


def test_parse_time_accepts_all_forms():
    assert parse_time("1:12:40") == 4_360_000
    assert parse_time("12:40") == 760_000
    assert parse_time("4360") == 4_360_000


def test_video_url_truncates_to_seconds():
    assert video_url("abc123", 61_999) == "https://www.youtube.com/watch?v=abc123&t=61s"
