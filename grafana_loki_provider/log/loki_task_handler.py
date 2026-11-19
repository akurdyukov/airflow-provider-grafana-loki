"""Loki logging handler for Airflow task logs.

Airflow 2: ``LokiTaskHandler`` uploads the local log file on ``close()`` and reads it back
in ``_read``.

Airflow 3: the task supervisor uploads via the ``REMOTE_TASK_LOG`` object defined in the logging
config module (a ``LokiRemoteLogIO``), and the API server reads through it. ``LokiTaskHandler``
delegates its remote reads to the same IO class.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from airflow.configuration import conf
from airflow.utils.log.file_task_handler import FileTaskHandler
from airflow.utils.log.logging_mixin import LoggingMixin

from grafana_loki_provider.hooks.loki import LokiHook
from grafana_loki_provider.version_compat import AIRFLOW_V_3_0_PLUS

if TYPE_CHECKING:
    from airflow.models import TaskInstance

logging.raiseExceptions = True

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
QUERY_LIMIT = 5000

# (timestamp in ns, line)
LogEntry = Tuple[int, str]


def get_labels(ti) -> Dict[str, str]:
    """Indexed Loki stream labels."""
    return {"dag_id": ti.dag_id, "task_id": ti.task_id, "application": "airflow"}


def get_structured_metadata(ti, try_number: Optional[int] = None) -> Dict[str, str]:
    """Per-line Loki structured metadata. Loki only accepts string values here."""
    return dict(
        run_id=str(getattr(ti, "run_id", "")),
        try_number=str(try_number if try_number is not None else ti.try_number),
        map_index=str(getattr(ti, "map_index", "")),
    )


def build_query(ti, try_number: int) -> str:
    """LogQL query for one task try.

    dag_id/task_id are stream labels and go in the stream selector. run_id/try_number/map_index
    are structured metadata, which Loki only matches via label filters.
    """
    return (
        '{{dag_id="{dag_id}",task_id="{task_id}"}}'
        ' | try_number="{try_number}"'
        ' | map_index="{map_index}"'
        ' | run_id="{run_id}"'
    ).format(
        dag_id=ti.dag_id,
        task_id=ti.task_id,
        try_number=try_number,
        map_index=getattr(ti, "map_index", ""),
        run_id=getattr(ti, "run_id", ""),
    )


def build_query_params(ti, try_number: int) -> Dict[str, Any]:
    start_date = ti.start_date or datetime.now(timezone.utc)
    # A running or queued task has no end_date yet; use a generous window instead.
    end_date = ti.end_date or start_date + timedelta(days=5)
    return {
        "query": build_query(ti, try_number),
        "start": (start_date - timedelta(days=15)).isoformat(),
        "end": (end_date + timedelta(hours=1)).isoformat(),
        "limit": QUERY_LIMIT,
        "direction": "forward",
    }


def build_payload(
    entries: List[LogEntry], labels: Dict[str, str], metadata: Dict[str, str]
) -> dict:
    """Build a Loki push payload. ANSI escape sequences are stripped (Loki doesn't support them)."""
    values = [[str(ts), ANSI_ESCAPE.sub("", line), metadata] for ts, line in entries]
    return {"streams": [{"stream": labels, "values": values}]}


def encode_payload(payload: dict, enable_gzip: bool) -> Tuple[bytes, Dict[str, str]]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if enable_gzip:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    return body, headers


def extract_lines(data: dict) -> List[str]:
    """Pull log lines out of a Loki query_range response, in stream order."""
    lines = []
    for stream in data.get("data", {}).get("result", []):
        for value in stream.get("values", []):
            if len(value) > 1:
                lines.append(value[1])
    return lines


def _default_remote_conn_id() -> Optional[str]:
    if AIRFLOW_V_3_0_PLUS:
        # [logging] remote_log_conn_id, else DEFAULT_REMOTE_CONN_ID from the logging config module.
        # The task supervisor pre-fetches exactly this connection for the upload.
        try:
            from airflow.logging_config import get_default_remote_conn_id

            return get_default_remote_conn_id()
        except Exception:
            pass
    return conf.get("logging", "REMOTE_LOG_CONN_ID", fallback=None)


def get_hook(conn_id: Optional[str] = None) -> LokiHook:
    conn_id = conn_id or _default_remote_conn_id()
    return LokiHook(loki_conn_id=conn_id or LokiHook.default_conn_name)


def _parse_timestamp_ns(line: str) -> Optional[int]:
    """Timestamp of an Airflow 3 JSON log line, in ns since epoch; None if it has none."""
    try:
        ts = json.loads(line).get("timestamp")
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 10**9 + dt.microsecond * 1000


def to_entries(
    lines: List[str], last_ts: Optional[int] = None
) -> Tuple[List[LogEntry], Optional[int]]:
    """Timestamp lines; ones without their own timestamp inherit the previous line's.

    Returns the entries and the last timestamp, so a caller can continue across chunks.
    """
    entries: List[LogEntry] = []
    for line in lines:
        if not line:
            continue
        ts = _parse_timestamp_ns(line) or last_ts or time.time_ns()
        entries.append((ts, line))
        last_ts = ts
    return entries, last_ts


# Keep each push well under Loki's default 4 MiB request limit.
MAX_BATCH_ENTRIES = 1000
MAX_BATCH_BYTES = 1024 * 1024


def iter_batches(entries: List[LogEntry]):
    batch: List[LogEntry] = []
    size = 0
    for entry in entries:
        line_size = len(entry[1].encode("utf-8", errors="replace"))
        if batch and (len(batch) >= MAX_BATCH_ENTRIES or size + line_size > MAX_BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(entry)
        size += line_size
    if batch:
        yield batch


_PATH_SEGMENT = re.compile(r"^(dag_id|run_id|task_id|map_index)=(.+)$")
_ATTEMPT_FILE = re.compile(r"^attempt=(\d+)\.log$")


def parse_log_path(relative_path: str | os.PathLike):
    """Identify the task try from a log path rendered by Airflow's default log_filename_template.

    ``dag_id=<d>/run_id=<r>/task_id=<t>/[map_index=<m>/]attempt=<n>.log``. Returns an object with
    the task-instance fields used for labels, or None if the path doesn't follow that layout.
    """
    parts = Path(relative_path).parts
    if not parts or not (attempt := _ATTEMPT_FILE.match(parts[-1])):
        return None
    fields = {}
    for part in parts[:-1]:
        if match := _PATH_SEGMENT.match(part):
            fields[match.group(1)] = match.group(2)
    if not {"dag_id", "run_id", "task_id"} <= fields.keys():
        return None
    return SimpleNamespace(
        dag_id=fields["dag_id"],
        task_id=fields["task_id"],
        run_id=fields["run_id"],
        map_index=int(fields.get("map_index", -1)),
        try_number=int(attempt.group(1)),
    )


class _LogStreamer:
    """Ships complete lines appended to one task log file, from a background thread.

    Tracks the byte offset of what Loki has accepted, so the final flush in ``upload()`` sends
    only the rest, and lines from a failed push are retried rather than lost or duplicated.
    """

    def __init__(self, io: "LokiRemoteLogIO", path: Path, ti, interval: float):
        self.io = io
        self.path = path
        self.ti = ti
        self.interval = interval
        # Anything already in the file (e.g. the segment before a deferral) was uploaded already.
        self.offset = path.stat().st_size if path.exists() else 0
        self.last_ts: Optional[int] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"loki-stream-{path.name}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.interval + 10)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.flush()
            except Exception:
                # Offset is unchanged; these lines are retried on the next tick or in upload().
                pass

    def flush(self, ti=None, final: bool = False) -> None:
        """Push new lines. Without ``final``, a trailing partial line waits for its newline."""
        with self._lock:
            if not self.path.exists():
                return
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
            if not final:
                data = data[: data.rfind(b"\n") + 1]
            if not data:
                return
            lines = data.decode("utf-8", errors="replace").splitlines()
            entries, last_ts = to_entries(lines, self.last_ts)
            self.io.push_entries(entries, ti or self.ti)
            self.offset += len(data)
            self.last_ts = last_ts


class LokiRemoteLogIO(LoggingMixin):
    """Remote log IO for Airflow 3 (``REMOTE_TASK_LOG``): uploads and reads task logs in Loki.

    :param base_log_folder: Local base log folder; relative upload paths resolve against it.
    :param delete_local_copy: Delete the local log file after a successful upload.
    :param enable_gzip: Gzip the push request body.
    :param conn_id: Loki connection id; defaults to ``[logging] remote_log_conn_id``.
    :param stream_logs: Ship log lines to Loki while the task runs, not only when it finishes.
        Needs log paths in Airflow's default ``log_filename_template`` layout; other logs fall
        back to the upload at task end.
    :param stream_interval: Seconds between streamed pushes.
    """

    def __init__(
        self,
        base_log_folder: str | os.PathLike,
        delete_local_copy: bool = False,
        enable_gzip: bool = True,
        conn_id: Optional[str] = None,
        stream_logs: bool = False,
        stream_interval: float = 2.0,
    ):
        super().__init__()
        self.base_log_folder = Path(base_log_folder)
        self.delete_local_copy = delete_local_copy
        self.enable_gzip = enable_gzip
        self.conn_id = conn_id
        self.stream_logs = stream_logs
        self.stream_interval = stream_interval
        self._streamers: Dict[str, _LogStreamer] = {}
        # File names already looked at by the processor; None means "not streamable".
        self._seen_files: Dict[str, Optional[_LogStreamer]] = {}
        self._streamers_lock = threading.Lock()
        self._push_lock = threading.Lock()

    @cached_property
    def hook(self) -> LokiHook:
        return get_hook(self.conn_id)

    @property
    def processors(self) -> tuple:
        if not self.stream_logs:
            return ()
        # Airflow reads this while the supervisor has the remote-logging connection in an env
        # var; capture it now, since streamer threads run after the env var is gone.
        try:
            self.hook.cache_connection()
        except Exception:
            self.log.warning("Loki: can't fetch the connection; streaming disabled", exc_info=True)
            return ()
        return (self._stream_processor,)

    def _stream_processor(self, logger, method_name, event):
        """structlog processor: start streaming a task log file on its first record."""
        try:
            name = getattr(getattr(logger, "_file", None), "name", None)
            if isinstance(name, str) and name not in self._seen_files:
                self._start_streamer(name)
        except Exception:
            # Never break task logging because of streaming.
            pass
        return event

    def _start_streamer(self, file_name: str) -> None:
        with self._streamers_lock:
            if file_name in self._seen_files:
                return
            streamer = None
            path = Path(file_name).resolve()
            try:
                relative = path.relative_to(self.base_log_folder.resolve())
            except ValueError:
                relative = None
            ti = parse_log_path(relative) if relative is not None else None
            if ti is not None:
                streamer = _LogStreamer(self, path, ti, self.stream_interval)
                self._streamers[str(path)] = streamer
                streamer.start()
            self._seen_files[file_name] = streamer

    def push_entries(self, entries: List[LogEntry], ti) -> None:
        labels, metadata = get_labels(ti), get_structured_metadata(ti)
        with self._push_lock:  # HttpHook isn't thread-safe (it mutates self.method)
            for batch in iter_batches(entries):
                payload = build_payload(batch, labels, metadata)
                body, headers = encode_payload(payload, self.enable_gzip)
                self.hook.push_log(payload=body, headers=headers)

    def upload(self, path: str | os.PathLike, ti=None) -> None:
        local_path = Path(path)
        if not local_path.is_absolute():
            local_path = self.base_log_folder / local_path

        with self._streamers_lock:
            streamer = self._streamers.pop(str(local_path.resolve()), None)
            if streamer is not None:
                self._seen_files = {k: v for k, v in self._seen_files.items() if v is not streamer}
        if streamer is not None:
            # Already streamed up to streamer.offset; send the rest (raises if Loki rejects it,
            # which leaves the local file in place).
            streamer.stop()
            streamer.flush(ti, final=True)
        else:
            if ti is None:
                # Not a task log (e.g. a callback); there are no task labels to file it under.
                return
            if not local_path.is_file():
                return
            entries, _ = to_entries(local_path.read_text(errors="replace").splitlines())
            self.push_entries(entries, ti)

        if self.delete_local_copy:
            local_path.unlink(missing_ok=True)

    def read(self, relative_path: str, ti) -> Tuple[List[str], Optional[List[str]]]:
        if ti is None:
            return [], None
        return self.read_try(ti, ti.try_number)

    def read_try(self, ti, try_number: int) -> Tuple[List[str], Optional[List[str]]]:
        params = build_query_params(ti, try_number)
        messages = [f"Reading remote log from Loki: {params['query']}"]
        try:
            lines = extract_lines(self.hook.query_range(params))
        except Exception as e:
            messages.append(f"Failed to read logs from Loki: {e}")
            return messages, None
        if not lines:
            return messages, None
        return messages, ["\n".join(line.rstrip("\n") for line in lines) + "\n"]


class LokiTaskHandler(FileTaskHandler, LoggingMixin):
    def __init__(
        self,
        base_log_folder,
        name,
        filename_template: Optional[str] = None,
        enable_gzip=True,
    ):
        if AIRFLOW_V_3_0_PLUS:
            super().__init__(base_log_folder)
        else:
            super().__init__(base_log_folder, filename_template)
        self.name: str = name
        self.handler: Optional[logging.FileHandler] = None
        self.log_relative_path = ""
        self.closed = False
        self.upload_on_close = True
        self.enable_gzip = enable_gzip
        self.labels: Dict[str, str] = {}
        self.extras: Dict[str, str] = {}
        self.io = LokiRemoteLogIO(base_log_folder=base_log_folder, enable_gzip=enable_gzip)

    @property
    def hook(self) -> LokiHook:
        """Returns LokiHook"""
        return self.io.hook

    def get_extras(self, ti, try_number=None) -> Dict[str, str]:
        return get_structured_metadata(ti, try_number)

    def get_labels(self, ti) -> Dict[str, str]:
        return get_labels(ti)

    def _get_task_query(self, ti, try_number, metadata) -> str:
        return build_query(ti, try_number)

    if AIRFLOW_V_3_0_PLUS:

        def _read_remote_logs(self, ti, try_number, metadata=None):
            return self.io.read_try(ti, try_number)

    else:

        def set_context(self, task_instance: "TaskInstance", **kwargs) -> None:
            super().set_context(task_instance, **kwargs)

            ti = task_instance

            self.log_relative_path = self._render_filename(ti, ti.try_number)
            self.upload_on_close = not ti.raw

            # Clear the file first so that duplicate data is not uploaded
            # when re-using the same path (e.g. with rescheduled sensors)
            if self.upload_on_close:
                if self.handler:
                    with open(self.handler.baseFilename, "w"):
                        pass
            self.labels = self.get_labels(ti)
            self.extras = self.get_extras(ti)

        def _read(
            self, ti, try_number: int, metadata: Optional[str] = None
        ) -> Tuple[str, Dict[str, bool]]:
            params = build_query_params(ti, try_number)
            self.log.info(f"loki log query params {params}")
            lines = extract_lines(self.hook.query_range(params))

            if lines:
                return "".join(lines), {"end_of_log": True}
            return super()._read(ti, try_number, metadata)

        def close(self):
            """Close and upload local log file to remote storage Loki."""

            if self.closed:
                return

            super().close()

            if not self.upload_on_close:
                return

            local_loc = os.path.join(self.local_base, self.log_relative_path)
            if os.path.exists(local_loc):
                with open(local_loc) as logfile:
                    log = logfile.readlines()
                self.loki_write(log)

            # Mark closed so we don't double write if close is called twice
            self.closed = True

    def build_payload(self, log: List[str], labels, extras) -> dict:
        """Build JSON payload, timestamping each line at upload time (Airflow 2 text logs)."""
        entries = [(int(time.time() * 1e9), line) for line in log]
        return build_payload(entries, labels, extras)

    def loki_write(self, log: List[str]):
        body, headers = encode_payload(
            self.build_payload(log, self.labels, self.extras), self.enable_gzip
        )
        self.hook.push_log(payload=body, headers=headers)
