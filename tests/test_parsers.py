from datetime import datetime

import pytest

from sfestimate.parsers import load, parse_bigquery, parse_postgres, parse_redshift

from conftest import write_csv

RS_HEADER = ["query_id", "user_id", "query_type", "status", "start_time", "end_time",
             "elapsed_time", "result_cache_hit", "scanned_bytes"]
BQ_HEADER = ["job_id", "creation_time", "start_time", "end_time", "user_email", "job_type",
             "statement_type", "total_bytes_processed", "total_slot_ms", "cache_hit",
             "state", "error_result"]
PG_HEADER = ["userid", "queryid", "query", "calls", "total_exec_time", "mean_exec_time",
             "rows", "shared_blks_hit", "shared_blks_read"]


# --- Redshift -----------------------------------------------------------------

def test_redshift_parses_and_filters(tmp_path):
    p = write_csv(tmp_path / "rs.csv", RS_HEADER, [
        [1, 100, "SELECT", "success", "2026-01-05 10:00:00.123456", "2026-01-05 10:00:02.623456",
         2_500_000, "f", 1_000_000],
        [2, 100, "INSERT", "success", "2026-01-05 01:00:00", "2026-01-05 01:01:00",
         60_000_000, "f", 5_000_000_000],
        [3, 100, "SELECT", "failed", "2026-01-05 10:00:00", "2026-01-05 10:00:01", 1_000_000, "f", 0],
        [4, 100, "SELECT", "canceled", "2026-01-05 10:00:00", "2026-01-05 10:00:01", 1_000_000, "f", 0],
        [5, 100, "UTILITY", "success", "2026-01-05 10:00:00", "2026-01-05 10:00:01", 1_000, "f", 0],
        [6, 100, "DDL", "success", "2026-01-05 10:00:00", "2026-01-05 10:00:01", 1_000, "f", 0],
        [7, 100, "select", "success", "2026-01-05 11:00:00", "2026-01-05 11:00:00.1",
         100_000, "t", 0],
    ])
    events = parse_redshift(p)
    assert [e.query_id for e in events] == ["1", "2", "7"]
    e1, e2, e7 = events
    assert e1.duration_s == pytest.approx(2.5)  # microseconds -> seconds
    assert e1.bytes_scanned == 1_000_000
    assert e1.start == datetime(2026, 1, 5, 10, 0, 0, 123456)
    assert e1.query_type == "SELECT" and not e1.cache_hit
    assert e2.query_type == "INSERT" and e2.duration_s == pytest.approx(60.0)
    assert e7.query_type == "SELECT" and e7.cache_hit


def test_redshift_missing_column(tmp_path):
    p = write_csv(tmp_path / "rs.csv", ["query_id", "start_time", "query_type"],
                  [[1, "2026-01-05 10:00:00", "SELECT"]])
    with pytest.raises(ValueError) as exc:
        parse_redshift(p)
    msg = str(exc.value)
    assert "elapsed_time" in msg and "export_queries/redshift.sql" in msg


def test_column_names_case_insensitive(tmp_path):
    p = write_csv(tmp_path / "rs.csv", [h.upper() for h in RS_HEADER], [
        [1, 100, "SELECT", "SUCCESS", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
         1_000_000, "f", 10],
    ])
    assert len(parse_redshift(p)) == 1


# --- BigQuery -----------------------------------------------------------------

def _bq(job_id, start="2026-01-05T10:00:00Z", end="2026-01-05T10:00:04Z", job_type="QUERY",
        stype="SELECT", processed=4_000_000_000, cache="false", state="DONE", error=""):
    return [job_id, start, start, end, "a@example.com", job_type, stype, processed, 1000,
            cache, state, error]


def test_bigquery_parses_and_filters(tmp_path):
    p = write_csv(tmp_path / "bq.csv", BQ_HEADER, [
        _bq("ok"),
        _bq("merge", stype="MERGE", start="2026-01-05T01:00:00.1234567Z",
            end="2026-01-05T01:00:30.1234567Z"),
        _bq("script", stype="SCRIPT"),
        _bq("load", job_type="LOAD"),
        _bq("err", error="invalidQuery"),
        _bq("running", state="RUNNING"),
        _bq("cached", processed=0, cache="true"),
        _bq("blank_type", stype=""),
    ])
    events = parse_bigquery(p)
    ids = [e.query_id for e in events]
    assert ids == ["ok", "merge", "cached", "blank_type"]
    ok, merge, cached, blank = events
    assert ok.duration_s == pytest.approx(4.0)
    assert ok.bytes_scanned == 4_000_000_000
    assert ok.user == "a@example.com"
    assert merge.query_type == "MERGE" and merge.duration_s == pytest.approx(30.0)
    assert cached.cache_hit and cached.bytes_scanned == 0
    assert blank.query_type == "SELECT"


def test_bigquery_negative_duration_clamped(tmp_path):
    p = write_csv(tmp_path / "bq.csv", BQ_HEADER, [
        _bq("x", start="2026-01-05T10:00:05Z", end="2026-01-05T10:00:00Z"),
    ])
    assert parse_bigquery(p)[0].duration_s == 0.0


def test_bigquery_missing_column(tmp_path):
    p = write_csv(tmp_path / "bq.csv", ["job_id", "start_time", "end_time"],
                  [["j", "2026-01-05T10:00:00Z", "2026-01-05T10:00:01Z"]])
    with pytest.raises(ValueError) as exc:
        parse_bigquery(p)
    assert "total_bytes_processed" in str(exc.value)
    assert "export_queries/bigquery.sql" in str(exc.value)


# --- Postgres -----------------------------------------------------------------

def test_postgres_filters_and_weights(tmp_path):
    p = write_csv(tmp_path / "pg.csv", PG_HEADER, [
        [10, 1, "SELECT a FROM t WHERE id = $1", 1000, 2000.0, 2.0, 1000, 600, 400],
        [10, 2, "INSERT INTO m SELECT * FROM s", 50, 50000.0, 1000.0, 50, 0, 100],
        [10, 3, "WITH x AS (SELECT 1) SELECT * FROM x", 3, 30.0, 10.0, 3, 0, 0],
        [10, 4, "BEGIN", 5000, 5.0, 0.001, 0, 0, 0],
        [10, 5, "SET search_path = public", 100, 1.0, 0.01, 0, 0, 0],
        [10, 6, "commit", 5000, 5.0, 0.001, 0, 0, 0],
        [10, 7, "CREATE INDEX i ON t(a)", 1, 100.0, 100.0, 0, 0, 0],
        [10, 8, "SELECT 1", 0, 0.0, 0.0, 0, 0, 0],
        [10, 9, "CREATE TABLE z AS SELECT * FROM t", 2, 200.0, 100.0, 0, 0, 0],
    ])
    events = parse_postgres(p, window_days=14, end=datetime(2026, 1, 19),
                            max_events_per_statement=200)
    by_stmt: dict[str, list] = {}
    for e in events:
        by_stmt.setdefault(e.query_id.split("-")[0], []).append(e)
    assert set(by_stmt) == {"1", "2", "3", "9"}

    # Each statement's weights add up to its calls.
    assert sum(e.weight for e in by_stmt["1"]) == pytest.approx(1000)
    assert len(by_stmt["1"]) == 200
    assert sum(e.weight for e in by_stmt["2"]) == pytest.approx(50)
    assert len(by_stmt["2"]) == 50
    assert sum(e.weight for e in by_stmt["3"]) == pytest.approx(3)

    sel = by_stmt["1"][0]
    assert sel.query_type == "SELECT"
    assert sel.duration_s == pytest.approx(0.002)            # 2000 ms / 1000 calls
    assert sel.bytes_scanned == int(1000 * 8192 / 1000)
    assert by_stmt["2"][0].query_type == "INSERT"
    assert by_stmt["3"][0].query_type == "SELECT"
    assert by_stmt["9"][0].query_type == "CTAS"

    start, end = datetime(2026, 1, 5), datetime(2026, 1, 19)
    for e in events:
        assert start <= e.start < end
    # Reads land in weekday business hours, writes overnight.
    for e in by_stmt["1"]:
        assert e.start.weekday() < 5 and 8 <= e.start.hour < 18
    for e in by_stmt["2"]:
        assert 1 <= e.start.hour < 5


def test_postgres_missing_column(tmp_path):
    p = write_csv(tmp_path / "pg.csv", ["queryid", "query", "calls"], [[1, "SELECT 1", 1]])
    with pytest.raises(ValueError) as exc:
        parse_postgres(p)
    assert "total_exec_time" in str(exc.value)
    assert "export_queries/postgres.sql" in str(exc.value)


# --- load() -------------------------------------------------------------------

def test_load_unknown_source(tmp_path):
    with pytest.raises(ValueError, match="Unknown source"):
        load("oracle", tmp_path / "x.csv")


def test_load_empty_raises(tmp_path):
    p = write_csv(tmp_path / "rs.csv", RS_HEADER, [
        [1, 100, "SELECT", "failed", "2026-01-05 10:00:00", "2026-01-05 10:00:01", 1, "f", 0],
    ])
    with pytest.raises(ValueError, match="No usable queries"):
        load("redshift", p)


def test_load_sorts_by_start(tmp_path):
    p = write_csv(tmp_path / "rs.csv", RS_HEADER, [
        [2, 1, "SELECT", "success", "2026-01-05 12:00:00", "2026-01-05 12:00:01", 1, "f", 0],
        [1, 1, "SELECT", "success", "2026-01-05 10:00:00", "2026-01-05 10:00:01", 1, "f", 0],
    ])
    assert [e.query_id for e in load("redshift", p)] == ["1", "2"]
