from dmr_hearings.pipeline.chunks import cases_at, make_chunks


def segments(count, step_ms=5_000):
    return [(i * step_ms, i * step_ms + step_ms, f"line {i}") for i in range(count)]


def test_cases_at_returns_all_overlapping_cases():
    spans = [(0, 10_000, "30001"), (5_000, 20_000, "30002")]
    assert cases_at(7_000, spans) == {"30001", "30002"}
    assert cases_at(15_000, spans) == {"30002"}
    assert cases_at(30_000, spans) == frozenset()


def test_chunks_never_cross_a_case_boundary():
    segs = segments(60)  # five minutes
    spans = [(0, 149_999, "30001"), (150_000, 300_000, "30002")]
    chunks = make_chunks(segs, spans)
    assert {tuple(c["cases"]) for c in chunks} == {("30001",), ("30002",)}
    for chunk in chunks:
        numbers = {int(w) for w in chunk["text"].split() if w.isdigit()}
        starts = {n * 5_000 for n in numbers}
        case = chunk["cases"][0]
        assert all((s < 150_000) == (case == "30001") for s in starts)


def test_chunks_overlap_and_cover_everything():
    segs = segments(60)
    chunks = make_chunks(segs, [])
    assert chunks[0]["start_ms"] == 0
    assert all(b["start_ms"] - a["start_ms"] <= 60_000 for a, b in zip(chunks, chunks[1:], strict=False))
    covered = " ".join(c["text"] for c in chunks)
    assert all(text in covered for _, _, text in segs)


def test_short_tail_is_merged_into_previous_chunk():
    chunks = make_chunks(segments(14), [])  # 70 seconds: one window plus a 10 second leftover
    assert len(chunks) == 1
    assert "line 13" in chunks[0]["text"]
