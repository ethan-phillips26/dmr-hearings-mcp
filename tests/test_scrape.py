import pytest

from dmr_hearings.pipeline.scrape import parse_date, parse_json3


@pytest.mark.parametrize(
    "title, expected",
    [
        ("8/27/2026 Hearings", "2026-08-27"),
        ("12/6/2022  Oil & Gas Hearing", "2022-12-06"),
        ("Dec 16, 2021 Hearings", "2021-12-16"),
        ("December 16, 2021 Hearings", "2021-12-16"),
        ("Hearing recap", None),
    ],
)
def test_parse_date(title, expected):
    assert parse_date(title) == expected


def test_parse_json3_joins_segments_and_drops_empty_events():
    data = {
        "events": [
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "good "}, {"utf8": "morning"}]},
            {"tStartMs": 1000, "dDurationMs": 500, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 1500},  # no segs at all
            {"tStartMs": 2000, "segs": [{"utf8": " next   case "}]},
        ]
    }
    assert parse_json3(data) == [(0, 1000, "good morning"), (2000, 0, "next case")]
