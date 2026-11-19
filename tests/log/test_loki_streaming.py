"""Unit tests for streaming task logs to Loki while the task runs (LokiRemoteLogIO.stream_logs)."""
import json
import time
from types import SimpleNamespace

import pytest
import requests_mock

from grafana_loki_provider.log import loki_task_handler
from grafana_loki_provider.log.loki_task_handler import LokiRemoteLogIO, parse_log_path

REL = "dag_id=d1/run_id=r1/task_id=t1/attempt=1.log"
METADATA = {"run_id": "r1", "try_number": "1", "map_index": "-1"}


def line(n):
    return json.dumps({"timestamp": f"2026-01-01T00:00:{n:02d}Z", "event": f"line {n}"})


@pytest.fixture
def io(tmp_path):
    io = LokiRemoteLogIO(
        base_log_folder=tmp_path, enable_gzip=False, stream_logs=True, stream_interval=3600
    )
    yield io
    for streamer in list(io._streamers.values()):
        streamer.stop()


@pytest.fixture
def log_file(tmp_path):
    path = tmp_path / REL
    path.parent.mkdir(parents=True)
    path.touch()
    with open(path, "ab") as fh:
        yield path, fh


def append(fh, text):
    fh.write(text.encode())
    fh.flush()


def pushed_lines(m):
    lines = []
    for request in m.request_history:
        for stream in json.loads(request.body)["streams"]:
            lines += [value[1] for value in stream["values"]]
    return lines


@pytest.mark.parametrize(
    "path, expected",
    [
        (REL, dict(dag_id="d1", run_id="r1", task_id="t1", map_index=-1, try_number=1)),
        (
            "dag_id=d/run_id=manual__2026-01-01T00:00:00+00:00/task_id=t/map_index=3/attempt=2.log",
            dict(
                dag_id="d",
                run_id="manual__2026-01-01T00:00:00+00:00",
                task_id="t",
                map_index=3,
                try_number=2,
            ),
        ),
        ("dag_processor/2026-01-01/dags.log", None),
        ("d1/t1/r1/1.log", None),
        ("dag_id=d1/task_id=t1/attempt=1.log", None),
    ],
)
def test_parse_log_path(path, expected):
    parsed = parse_log_path(path)
    assert (vars(parsed) if parsed else None) == expected


def test_processors_off_by_default(tmp_path):
    assert LokiRemoteLogIO(base_log_folder=tmp_path).processors == ()


def test_processors_cache_connection(io, mocker):
    cache = mocker.patch.object(io.hook, "cache_connection")
    assert len(io.processors) == 1
    cache.assert_called_once()


def test_processors_disabled_if_connection_unavailable(io, mocker):
    mocker.patch.object(io.hook, "cache_connection", side_effect=Exception("no conn"))
    assert io.processors == ()


def test_processor_ignores_non_task_logs(io, tmp_path):
    other = tmp_path / "dag_processor" / "x.log"
    other.parent.mkdir()
    outside = tmp_path.parent / "outside.log"
    with open(other, "ab") as f1, open(outside, "ab") as f2:
        for logger in (SimpleNamespace(), SimpleNamespace(_file=f1), SimpleNamespace(_file=f2)):
            event = {"event": "x"}
            assert io._stream_processor(logger, "info", event) is event
    assert io._streamers == {}


def test_streams_complete_lines_then_upload_sends_only_the_rest(io, log_file, loki_urls):
    path, fh = log_file
    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], status_code=204)
        io._stream_processor(SimpleNamespace(_file=fh), "info", {})
        streamer = io._streamers[str(path.resolve())]

        append(fh, f"{line(1)}\n{line(2)}\n{line(3)[:10]}")  # line 3 is still being written
        streamer.flush()
        assert pushed_lines(m) == [line(1), line(2)]
        stream = json.loads(m.last_request.body)["streams"][0]
        assert stream["stream"] == {"dag_id": "d1", "task_id": "t1", "application": "airflow"}
        assert stream["values"][0][2] == METADATA

        append(fh, f"{line(3)[10:]}\n{line(4)}\n")
        streamer.flush()
        append(fh, line(5))  # no trailing newline: only sent by the final flush

        ti = SimpleNamespace(dag_id="d1", task_id="t1", run_id="r1", try_number=1, map_index=-1)
        io.upload(REL, ti)

    assert pushed_lines(m) == [line(n) for n in range(1, 6)]
    assert io._streamers == {}
    assert not streamer._thread.is_alive()


def test_failed_push_is_retried_without_loss_or_duplicates(io, log_file, loki_urls):
    path, fh = log_file
    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], [{"status_code": 500}, {"status_code": 204}])
        io._stream_processor(SimpleNamespace(_file=fh), "info", {})
        streamer = io._streamers[str(path.resolve())]
        append(fh, f"{line(1)}\n")

        with pytest.raises(Exception):
            streamer.flush()
        assert streamer.offset == 0
        streamer.flush()

    assert [len(json.loads(r.body)["streams"][0]["values"]) for r in m.request_history] == [1, 1]
    assert streamer.offset == path.stat().st_size


def test_background_thread_pushes_while_running(tmp_path, log_file, loki_urls):
    path, fh = log_file
    io = LokiRemoteLogIO(base_log_folder=tmp_path, stream_logs=True, stream_interval=0.05)
    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], status_code=204)
        io._stream_processor(SimpleNamespace(_file=fh), "info", {})
        append(fh, f"{line(1)}\n")
        deadline = time.monotonic() + 5
        while not m.called and time.monotonic() < deadline:
            time.sleep(0.02)
        assert m.called, "streamer thread didn't push within 5s"
        io.upload(REL, None)  # nothing left to send
    assert m.call_count == 1


def test_existing_content_is_not_resent(io, log_file, loki_urls):
    """A resumed task (e.g. after deferral) appends to a file whose start was uploaded already."""
    path, fh = log_file
    append(fh, f"{line(1)}\n")
    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], status_code=204)
        io._stream_processor(SimpleNamespace(_file=fh), "info", {})
        append(fh, f"{line(2)}\n")
        io.upload(REL, None)
    assert pushed_lines(m) == [line(2)]


def test_upload_deletes_local_copy_after_streaming(tmp_path, log_file, loki_urls):
    path, fh = log_file
    io = LokiRemoteLogIO(
        base_log_folder=tmp_path, stream_logs=True, stream_interval=3600, delete_local_copy=True
    )
    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], status_code=204)
        io._stream_processor(SimpleNamespace(_file=fh), "info", {})
        append(fh, f"{line(1)}\n")
        io.upload(REL, None)
    assert not path.exists()


def test_large_uploads_are_batched(io, tmp_path, loki_urls, monkeypatch):
    monkeypatch.setattr(loki_task_handler, "MAX_BATCH_ENTRIES", 2)
    path = tmp_path / "plain.log"
    path.write_text("".join(f"{line(n)}\n" for n in range(5)))
    ti = SimpleNamespace(dag_id="d1", task_id="t1", run_id="r1", try_number=1, map_index=-1)
    with requests_mock.Mocker() as m:
        m.post(loki_urls["push_log_url"], status_code=204)
        io.upload("plain.log", ti)
    assert m.call_count == 3
    assert pushed_lines(m) == [line(n) for n in range(5)]
