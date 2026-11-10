from dmr_hearings.pipeline.cases import Transcript, find_mentions, merge_digits, repairs, split_hearing


def test_merge_digits_rejoins_split_number_without_swallowing_the_next():
    assert merge_digits("305 39") == "30539"
    assert merge_digits("33086 33087") == "33086"


def test_repairs_fixes_one_extra_or_missing_digit_from_a_known_pool():
    pool = {"31300", "33100"}
    assert repairs("313000", pool) == {"31300"}
    assert repairs("3310", pool) == {"33100"}
    assert repairs("99999", pool) == set()


def test_spoken_digit_runs_become_case_numbers():
    t = Transcript([(0, "we will hear case number three three zero eight six now")])
    assert "33086" in t.text
    mentions = find_mentions(t)
    assert [m.raw for m in mentions] == ["33086"]


def test_spoken_runs_that_arent_case_numbers_are_left_alone():
    t = Transcript([(0, "the well is at one two three four five feet")])  # starts with 1, not 2 or 3
    assert "12345" not in t.text


HEARING = [
    (0, "good morning this hearing will come to order"),
    (10_000, "the following cases are continued case number 33001 is continued"),
    (60_000, "next case on the docket is case number 33086 application of Murex"),
    (90_000, "the witness described the proposed spacing unit"),
    (120_000, "that concludes the hearing for case number 33086"),
    (130_000, "next case is case number 33087"),
    (170_000, "we will close the hearing in case number 33087"),
]


def test_split_hearing_finds_spans_and_status():
    cases, _, problems, _ = split_hearing(HEARING, docket=["33001", "33086", "33087"])
    by_number = {c.number: c for c in cases}

    assert by_number["33001"].status == "continued"
    assert by_number["33001"].start_ms is None

    heard = by_number["33086"]
    assert heard.status == "heard"
    assert heard.confidence == "high"
    assert 60_000 <= heard.start_ms <= 60_000 and heard.end_ms == 120_000

    assert by_number["33087"].start_ms == 130_000
    assert not problems


def test_case_missing_from_the_docket_is_low_confidence():
    cases, _, problems, _ = split_hearing(HEARING, docket=["33086"])
    by_number = {c.number: c for c in cases}
    assert by_number["33086"].confidence == "high"
    assert by_number["33087"].confidence == "low"
    assert any("not on docket" in p for p in problems)
