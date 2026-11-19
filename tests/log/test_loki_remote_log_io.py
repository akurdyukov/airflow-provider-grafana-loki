"""Unit tests for LokiRemoteLogIO (the Airflow 3 REMOTE_TASK_LOG path); run on Airflow 2 and 3."""
import gzip
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests_mock

from grafana_loki_provider.log.loki_task_handler import (
    LokiRemoteLogIO,
    LokiTaskHandler,
    _parse_timestamp_ns,
    to_entries,
)
from grafana_loki_provider.version_compat import AIRFLOW_V_3_0_PLUS

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
METADATA = {"run_id": "r1", "try_number": "2", "map_index": "-1"}


def make_ti(**overrides):
    fields = dict(
        dag_id="d1",
        task_id="t1",
        run_id="r1",
        try_number=2,
        map_index=-1,
        start_date=START,
        end_date=START + timedelta(minutes=5),
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def ns(dt):
    return int(dt.timestamp()) * 10**9 + dt.microsecond * 1000


def json_line(dt, event):
    return json.dumps({"timestamp": dt.isoformat().replace("+00:00", "Z"), "event": event})


@pytest.fixture
def io(tmp_path):
    return LokiRemoteLogIO(base_log_folder=tmp_path, enable_gzip=False)


def write_log(tmp_path, rel, lines):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def test_processors_empty(io):
    assert io.processors == ()


def test_parse_timestamp_ns():
    dt = START + timedelta(seconds=3, microseconds=7)
    assert _parse_timestamp_ns(json_line(dt, "x")) == ns(dt)
    assert _parse_timestamp_ns("not json") is None
    assert _parse_timestamp_ns('{"event": "no timestamp"}') is None


def test_upload_pushes_lines_with_their_own_timestamps(io, tmp_path, loki_urls):
    t1, t2 = START + timedelta(seconds=1), START + timedelta(seconds=2)
    line1, line2 = json_line(t1, "first"), json_line(t2, "second")
    write_log(tmp_path, "d1/attempt=2.log", [line1, line2, "plain continuation", ""])

    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], status_code=204)
        io.upload("d1/attempt=2.log", make_ti())

    request = m.last_request
    assert request.headers["Content-Type"] == "application/json"
    assert "Content-Encoding" not in request.headers
    stream = json.loads(request.body)["streams"][0]
    assert stream["stream"] == {"dag_id": "d1", "task_id": "t1", "application": "airflow"}
    assert stream["values"] == [
        [str(ns(t1)), line1, METADATA],
        [str(ns(t2)), line2, METADATA],
        # No timestamp of its own: inherits the previous line's.
        [str(ns(t2)), "plain continuation", METADATA],
    ]


def test_leading_untimed_lines_take_first_timestamp():
    t1 = START + timedelta(seconds=1)
    entries, last = to_entries(["plain first", json_line(t1, "timed"), "plain after"])
    assert entries == [
        (ns(t1), "plain first"),
        (ns(t1), json_line(t1, "timed")),
        (ns(t1), "plain after"),
    ]
    assert last == ns(t1)
    assert to_entries([]) == ([], None)


def test_reupload_after_append_repeats_identical_entries(io, tmp_path, loki_urls):
    """A task that defers and resumes appends to its log file and the whole file is uploaded
    again. Entries must be byte-identical across uploads so Loki deduplicates them."""
    t1, t2 = START + timedelta(seconds=1), START + timedelta(seconds=2)
    path = write_log(tmp_path, "a.log", ["untimed preamble", json_line(t1, "before deferral")])

    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], status_code=204)
        io.upload("a.log", make_ti())
        with open(path, "a") as f:
            f.write(json_line(t2, "after resume") + "\n")
        io.upload("a.log", make_ti())

    first, second = (json.loads(r.body)["streams"][0]["values"] for r in m.request_history)
    assert second[: len(first)] == first
    assert [v[1] for v in second[len(first):]] == [json_line(t2, "after resume")]


def test_upload_gzip_and_absolute_path(tmp_path, loki_urls):
    io = LokiRemoteLogIO(base_log_folder=tmp_path, enable_gzip=True)
    path = write_log(tmp_path, "a.log", ["\x1b[01;31mred\x1b[00m"])

    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], status_code=204)
        io.upload(path, make_ti())

    assert m.last_request.headers["Content-Encoding"] == "gzip"
    payload = json.loads(gzip.decompress(m.last_request.body))
    assert payload["streams"][0]["values"][0][1] == "red"


def test_upload_deletes_local_copy(tmp_path, loki_urls):
    io = LokiRemoteLogIO(base_log_folder=tmp_path, delete_local_copy=True)
    path = write_log(tmp_path, "a.log", [json_line(START, "x")])

    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], status_code=204)
        io.upload("a.log", make_ti())

    assert not path.exists()


def test_upload_failure_keeps_local_copy(tmp_path, loki_urls):
    io = LokiRemoteLogIO(base_log_folder=tmp_path, delete_local_copy=True)
    path = write_log(tmp_path, "a.log", [json_line(START, "x")])

    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], status_code=400)
        with pytest.raises(Exception):
            io.upload("a.log", make_ti())

    assert path.exists()


def test_upload_noop_without_ti_or_file(io, tmp_path):
    write_log(tmp_path, "a.log", [json_line(START, "x")])
    with requests_mock.Mocker():  # any request would raise NoMockAddress
        io.upload("a.log", None)
        io.upload("missing.log", make_ti())


def test_read_returns_lines_and_queries_requested_try(io, loki_urls):
    values = [["1", '{"event": "a"}'], ["2", '{"event": "b"}']]
    response = {"data": {"result": [{"values": values}]}}

    with requests_mock.Mocker() as m:
        m.get(loki_urls["query_range_url"], json=response)
        messages, logs = io.read("ignored/path.log", make_ti())

    query = parse_qs(urlsplit(m.last_request.url).query)["query"][0]
    assert query == '{dag_id="d1",task_id="t1"} | try_number="2" | map_index="-1" | run_id="r1"'
    assert messages == [f"Reading remote log from Loki: {query}"]
    assert logs == ['{"event": "a"}\n{"event": "b"}\n']


def test_read_without_end_date_uses_window(io, loki_urls):
    with requests_mock.Mocker() as m:
        m.get(loki_urls["query_range_url"], json={"data": {"result": []}})
        messages, logs = io.read("p", make_ti(end_date=None))

    assert logs is None
    params = parse_qs(urlsplit(m.last_request.url).query)
    assert params["end"][0] == (START + timedelta(days=5, hours=1)).isoformat()


def test_read_error_is_reported_not_raised(io, loki_urls):
    with requests_mock.Mocker() as m:
        m.get(loki_urls["query_range_url"], status_code=500)
        messages, logs = io.read("p", make_ti())

    assert logs is None
    assert messages[-1].startswith("Failed to read logs from Loki:")


def test_read_without_ti(io):
    assert io.read("p", None) == ([], None)


@pytest.mark.skipif(not AIRFLOW_V_3_0_PLUS, reason="Airflow 3 read path")
def test_handler_read_remote_logs_uses_requested_try(tmp_path, loki_urls):
    handler = LokiTaskHandler(base_log_folder=str(tmp_path), name="task")

    with requests_mock.Mocker() as m:
        m.get(loki_urls["query_range_url"], json={"data": {"result": []}})
        handler._read_remote_logs(make_ti(try_number=3), try_number=1)

    query = parse_qs(urlsplit(m.last_request.url).query)["query"][0]
    assert 'try_number="1"' in query
