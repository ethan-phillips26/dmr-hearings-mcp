import pytest

from dmr_hearings import db, search


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    with conn:
        conn.execute(
            "INSERT INTO videos (video_id, title, hearing_date, duration, status) VALUES "
            "('vid1', '8/26/2026 Hearings', '2026-08-26', 7200, 'done')"
        )
        conn.executemany(
            "INSERT INTO segments (video_id, start_ms, dur_ms, text) VALUES (?, ?, ?, ?)",
            [
                ("vid1", 0, 5000, "opening remarks"),
                ("vid1", 60_000, 5000, "flaring of associated gas"),
                ("vid1", 120_000, 5000, "closing statement"),
            ],
        )
        conn.execute("INSERT INTO cases VALUES ('vid1', '33086', 'heard', 50000, 130000, 'high')")
        conn.execute("INSERT INTO cases VALUES ('vid1', '33001', 'continued', NULL, NULL, 'high')")
        conn.execute(
            "INSERT INTO docket_cases VALUES ('2026-08-26', '33086', 0, 'main', 'Murex Petroleum Corporation', "
            "'Application of Murex Petroleum Corporation for a flaring exception', NULL, 'http://x/docket.pdf')"
        )
        conn.execute(
            "INSERT INTO chunks (chunk_id, video_id, start_ms, end_ms, text) VALUES "
            "(1, 'vid1', 50000, 130000, 'the operator requests a flaring exception for gas capture')"
        )
        conn.execute("INSERT INTO chunk_cases VALUES (1, '33086')")
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        conn.execute("INSERT INTO docket_fts(docket_fts) VALUES ('rebuild')")
    yield conn
    conn.close()


def test_fts_query_quotes_words_and_drops_stopwords():
    assert search.fts_query("What is the flaring exception?") == '"flaring" OR "exception"'
    assert search.fts_query('foo" AND (bar') == '"foo" OR "bar"'  # FTS5 syntax can't leak through
    assert search.fts_query("the and of") is None


def test_keyword_search_stems_words(conn):
    assert search.keyword_search(conn, "flare", 10) == [1]  # porter stemming: flare ~ flaring
    assert search.keyword_search(conn, "flaring", 10, date_from="2027-01-01") == []
    assert search.keyword_search(conn, "flaring", 10, case_number="33086") == [1]
    assert search.keyword_search(conn, "flaring", 10, case_number="99999") == []


def test_search_dockets_reports_whether_case_was_heard(conn):
    rows = search.search_dockets(conn, "Murex flaring")
    assert len(rows) == 1
    assert rows[0]["case_number"] == "33086"
    assert rows[0]["heard"] == 1


def test_get_case_lists_every_hearing_with_links(conn):
    result = search.get_case(conn, "33086")
    assert result["dockets"][0]["applicant"] == "Murex Petroleum Corporation"
    hearing = result["hearings"][0]
    assert hearing["status"] == "heard"
    assert hearing["url"] == "https://www.youtube.com/watch?v=vid1&t=50s"
    assert "url" not in search.get_case(conn, "33001")["hearings"][0]  # continued: no span to link to


def test_read_transcript_returns_window_and_cases_heard(conn):
    result = search.read_transcript(conn, "vid1", 60_000, before_ms=10_000, after_ms=10_000)
    assert [line["text"] for line in result["lines"]] == ["flaring of associated gas"]
    assert [c["case_number"] for c in result["cases"]] == ["33086"]
    assert "error" in search.read_transcript(conn, "nope", 0)


def test_list_hearings_filters_by_date(conn):
    assert len(search.list_hearings(conn)) == 1
    assert search.list_hearings(conn, date_from="2027-01-01") == []
