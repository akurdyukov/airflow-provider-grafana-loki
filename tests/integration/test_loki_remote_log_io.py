"""LokiRemoteLogIO against a real Loki: upload a JSON-lines task log, read it back.

This is the Airflow 3 REMOTE_TASK_LOG path; the IO class itself runs on Airflow 2 and 3.
Run with LOKI_URL set, e.g.:
    LOKI_URL=http://localhost:3100 pytest tests/integration
"""
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from grafana_loki_provider.log.loki_task_handler import LokiRemoteLogIO

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("LOKI_URL"), reason="LOKI_URL not set"),
]


@pytest.fixture
def ti():
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        # Unique dag_id per test so streams from other tests/runs never match.
        dag_id=f"loki_io_it_{uuid.uuid4().hex[:10]}",
        task_id="it_task",
        run_id="it_run",
        try_number=1,
        map_index=-1,
        start_date=now - timedelta(minutes=1),
        end_date=now,
    )


def write_json_log(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    base = datetime.now(timezone.utc)
    with open(path, "w") as f:
        for i, event in enumerate(events):
            ts = (base + timedelta(milliseconds=i)).isoformat().replace("+00:00", "Z")
            f.write(json.dumps({"timestamp": ts, "event": event, "level": "info"}) + "\n")


def wait_for_events(io, ti, expected_count, timeout=20.0):
    deadline = time.monotonic() + timeout
    events = []
    while time.monotonic() < deadline:
        messages, logs = io.read("unused", ti)
        events = [json.loads(line)["event"] for line in (logs or [""])[0].splitlines()]
        if len(events) >= expected_count:
            return events
        time.sleep(0.5)
    pytest.fail(f"Loki returned {len(events)}/{expected_count} lines; last messages: {messages}")


@pytest.mark.parametrize("enable_gzip", [True, False], ids=["gzip", "plain"])
def test_upload_and_read_back(tmp_path, ti, enable_gzip):
    io = LokiRemoteLogIO(base_log_folder=tmp_path, enable_gzip=enable_gzip, delete_local_copy=True)
    rel = f"dag_id={ti.dag_id}/attempt=1.log"
    write_json_log(tmp_path / rel, ["first", "second", "third"])

    io.upload(rel, ti)

    assert not (tmp_path / rel).exists()
    assert wait_for_events(io, ti, 3) == ["first", "second", "third"]


def test_read_isolates_tries(tmp_path, ti):
    io = LokiRemoteLogIO(base_log_folder=tmp_path)
    write_json_log(tmp_path / "attempt=1.log", ["from try 1"])
    io.upload("attempt=1.log", ti)
    ti.try_number = 2
    write_json_log(tmp_path / "attempt=2.log", ["from try 2"])
    io.upload("attempt=2.log", ti)

    assert wait_for_events(io, ti, 1) == ["from try 2"]
    ti.try_number = 1
    assert wait_for_events(io, ti, 1) == ["from try 1"]


def test_streams_while_running_without_duplicates(tmp_path, ti):
    io = LokiRemoteLogIO(base_log_folder=tmp_path, stream_logs=True, stream_interval=0.2)
    rel = f"dag_id={ti.dag_id}/run_id={ti.run_id}/task_id={ti.task_id}/attempt=1.log"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.touch()

    def append(events):
        base = datetime.now(timezone.utc)
        with open(path, "a") as f:
            for i, event in enumerate(events):
                ts = (base + timedelta(milliseconds=i)).isoformat().replace("+00:00", "Z")
                f.write(json.dumps({"timestamp": ts, "event": event}) + "\n")

    with open(path, "ab") as fh:
        # What the supervisor's file logger does on the task's first log record.
        io._stream_processor(SimpleNamespace(_file=fh), "info", {})
        append(["running 1", "running 2"])

        # Visible in Loki while the task is still running (before upload()).
        assert wait_for_events(io, ti, 2) == ["running 1", "running 2"]

        append(["finishing"])
        io.upload(rel, ti)

    assert wait_for_events(io, ti, 3) == ["running 1", "running 2", "finishing"]
    time.sleep(1)  # give any stray duplicate push time to land
    assert wait_for_events(io, ti, 3) == ["running 1", "running 2", "finishing"]


def test_reupload_after_deferral_is_deduplicated(tmp_path, ti):
    """A deferred task resumes, appends to its log file, and the whole file is uploaded again.
    Entries are identical across uploads, so Loki deduplicates them, even after the first
    upload was flushed from the ingester to storage (a long deferral)."""
    import requests

    io = LokiRemoteLogIO(base_log_folder=tmp_path)
    path = tmp_path / "attempt=1.log"
    path.write_text("untimed preamble\n")
    write_json_log_append = [
        json.dumps({"timestamp": ts, "event": event})
        for ts, event in [
            ((ti.start_date + timedelta(seconds=1)).isoformat(), "before deferral"),
            ((ti.start_date + timedelta(seconds=2)).isoformat(), "after resume"),
        ]
    ]
    with open(path, "a") as f:
        f.write(write_json_log_append[0] + "\n")
    io.upload("attempt=1.log", ti)

    requests.post(f"{os.environ['LOKI_URL']}/flush", timeout=10).raise_for_status()

    with open(path, "a") as f:
        f.write(write_json_log_append[1] + "\n")
    io.upload("attempt=1.log", ti)

    deadline = time.monotonic() + 20
    lines = []
    while time.monotonic() < deadline:
        _, logs = io.read("unused", ti)
        lines = (logs or [""])[0].splitlines()
        if len(lines) >= 3:
            break
        time.sleep(0.5)
    time.sleep(1)
    _, logs = io.read("unused", ti)
    lines = (logs or [""])[0].splitlines()
    assert lines == ["untimed preamble", *write_json_log_append]
