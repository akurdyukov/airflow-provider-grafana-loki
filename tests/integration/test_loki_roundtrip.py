"""Round-trip tests against a real Loki: push via close(), read back via _read().

Run with LOKI_URL set, e.g.:
    LOKI_URL=http://localhost:3100 pytest tests/integration
"""
import logging
import os
import time
import uuid
from datetime import timedelta

import pytest
from airflow.models import DAG, DagRun, TaskInstance
from airflow.operators.empty import EmptyOperator
from airflow.utils import timezone
from airflow.utils.log.file_task_handler import FileTaskHandler
from airflow.utils.session import create_session
from airflow.utils.state import State

from grafana_loki_provider.log.loki_task_handler import LokiTaskHandler

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("LOKI_URL"), reason="LOKI_URL not set"),
]


@pytest.fixture
def ti():
    # Unique dag_id per test so streams from other tests/runs never match.
    dag_id = f"loki_it_{uuid.uuid4().hex[:10]}"
    now = timezone.utcnow()
    dag = DAG(dag_id, start_date=now - timedelta(days=1), schedule=None)
    task = EmptyOperator(task_id="it_task", dag=dag)
    dag_run = DagRun(dag_id=dag_id, execution_date=now, run_id="it_run", run_type="manual")
    with create_session() as session:
        session.add(dag_run)
        session.commit()
        session.refresh(dag_run)

    ti = TaskInstance(task=task, run_id=dag_run.run_id, map_index=-1)
    ti.dag_run = dag_run
    ti.try_number = 1
    ti.start_date = now - timedelta(minutes=1)
    ti.end_date = now
    ti.state = State.SUCCESS

    yield ti

    with create_session() as session:
        session.query(DagRun).filter(DagRun.dag_id == dag_id).delete()


@pytest.fixture(autouse=True)
def no_local_fallback(mocker):
    """Fail loudly if _read falls back to the local file instead of returning Loki data."""
    mocker.patch.object(
        FileTaskHandler, "_read", side_effect=AssertionError("fell back to local log file")
    )


def write_task_log(tmp_path, ti, messages, enable_gzip=True):
    handler = LokiTaskHandler(
        base_log_folder=str(tmp_path), name="it_handler", enable_gzip=enable_gzip
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.set_context(ti)
    for msg in messages:
        handler.emit(logging.LogRecord("airflow.task", logging.INFO, __file__, 0, msg, None, None))
    handler.close()
    return handler


def wait_for_lines(handler, ti, try_number, expected_count, timeout=20.0):
    """Poll Loki until the stream is queryable, then return _read output."""
    query = handler._get_task_query(ti, try_number, None)
    params = {
        "query": query,
        "start": (ti.start_date - timedelta(hours=1)).isoformat(),
        "end": (ti.end_date + timedelta(hours=1)).isoformat(),
        "limit": 5000,
    }
    deadline = time.monotonic() + timeout
    count = 0
    while time.monotonic() < deadline:
        data = handler.hook.query_range(params)
        count = sum(len(s["values"]) for s in data.get("data", {}).get("result", []))
        if count >= expected_count:
            return handler._read(ti, try_number)
        time.sleep(0.5)
    pytest.fail(f"Loki returned {count}/{expected_count} lines for {query!r} within {timeout}s")


@pytest.mark.parametrize("enable_gzip", [True, False], ids=["gzip", "plain"])
def test_push_and_read_back(tmp_path, ti, enable_gzip):
    messages = ["first line", "second line", "\x1b[01;31mcolored\x1b[00m line"]
    handler = write_task_log(tmp_path, ti, messages, enable_gzip=enable_gzip)

    log, meta = wait_for_lines(handler, ti, ti.try_number, len(messages))

    assert meta == {"end_of_log": True}
    assert [line.rstrip("\n") for line in log.splitlines()] == [
        "first line",
        "second line",
        "colored line",
    ]


def test_read_filters_by_try_number(tmp_path, ti):
    """run_id/try_number/map_index are structured metadata; reads must still isolate tries."""
    write_task_log(tmp_path / "try1", ti, ["from try 1"])
    ti.try_number = 2
    handler = write_task_log(tmp_path / "try2", ti, ["from try 2"])

    log1, _ = wait_for_lines(handler, ti, 1, 1)
    log2, _ = wait_for_lines(handler, ti, 2, 1)

    assert log1.strip() == "from try 1"
    assert log2.strip() == "from try 2"
