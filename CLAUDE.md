# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Airflow provider package (`airflow-provider-grafana-loki`) that ships Airflow task logs to Grafana Loki and reads them back for the Airflow UI. Supports Airflow 2 and 3, which use different logging paths (see Architecture). Components: `LokiHook` (HTTP client), `LokiRemoteLogIO` (Airflow 3 remote log IO) and `LokiTaskHandler` (logging handler).

## Commands

```bash
# Known-working envs: Python 3.11/3.12 with Airflow 2.11.2 or 3.4.0 (CI tests both)
uv venv .venv --python 3.12
uv pip install --python .venv -e ".[test]" "apache-airflow==2.11.2" apache-airflow-providers-http \
  --constraint https://raw.githubusercontent.com/apache/airflow/constraints-2.11.2/constraints-3.12.txt
# For Airflow 3, use a separate venv with "apache-airflow==3.4.0" and constraints-3.4.0
airflow db migrate                # required once before tests: Airflow 2 tests use the ORM (DagRun/TaskInstance)
                                  # set AIRFLOW_HOME to a throwaway dir to avoid touching ~/airflow
pytest                            # run all tests (integration tests skip unless LOKI_URL is set)
pytest tests/log/test_loki_task_handler.py::TestLokiHandler::test_read   # single test

# Integration tests against a real Loki (same as the CI `integration` jobs)
podman run -d --rm --name loki-it -p 3100:3100 docker.io/grafana/loki:3.4.2
LOKI_URL=http://localhost:3100 pytest tests/integration
flake8 grafana_loki_provider tests   # lint (max-line-length 100, configured in tox.ini)
```

## Architecture

- **Provider registration**: `grafana_loki_provider/__init__.py:get_provider_info` is exposed via the `apache_airflow_provider` entry point. It registers the logging handler and the `grafana_loki` connection type. **The package version lives here** (`"versions": [...]`) — hatch reads it via regex in `pyproject.toml`. `pyproject.toml` (hatchling) is the real build config; `setup.py` is legacy — keep its `version` and `install_requires` in sync when releasing.
- **Version gating**: `version_compat.AIRFLOW_V_3_0_PLUS` (same pattern as official providers). `LokiTaskHandler` defines different methods depending on it.
- **`LokiHook`** (`hooks/loki.py`): thin `HttpHook` wrapper; `query_range` (GET) and `push_log` (POST, 3s timeout) against `/loki/api/v1/{method}`. Mutates `self.method` per call.
- **`log/loki_task_handler.py`**: module-level helpers shared by both paths (`get_labels`, `get_structured_metadata`, `build_query`, `build_query_params`, `build_payload`, `encode_payload`, `extract_lines`, `get_hook`).
  - Stream **labels** (indexed): `dag_id`, `task_id`, `application="airflow"`.
  - **Structured metadata** (`run_id`, `try_number`, `map_index`) is the third element of each value tuple. Values must be strings: Loki rejects the whole push with 400 otherwise.
  - Queries use a stream selector on `dag_id`/`task_id`, then `| label="..."` filters for the structured metadata (it can't go in the `{...}` selector); window `start_date - 15d` to `end_date (or start+5d) + 1h`, limit 5000.
  - ANSI escape sequences are stripped before push. Bodies are always JSON, optionally gzipped.
- **Airflow 2 path** — `LokiTaskHandler` (extends `FileTaskHandler`): on `close()` reads the whole local log file and pushes it in one request (each line timestamped at upload time). Skips upload when `ti.raw`. `_read` queries Loki and falls back to `FileTaskHandler._read` if empty.
- **Airflow 3 path** — handlers don't upload. The task supervisor calls `REMOTE_TASK_LOG.upload(path, ti)` after the task finishes, and the API server's `FileTaskHandler._read_remote_logs` calls `REMOTE_TASK_LOG.read(path, ti)`. `REMOTE_TASK_LOG` is a module-level `LokiRemoteLogIO` in the user's `log_config.py`.
  - Local logs are JSON lines; `upload` pushes them verbatim, using each line's own `timestamp` (lines without one inherit the previous line's), in batches (`iter_batches`: ≤1000 lines / 1 MiB). Returns lines raw from `read` so Airflow parses them as structured logs.
  - **Streaming** (`stream_logs=True`, opt-in): `processors` returns one structlog processor, which Airflow installs in the *supervisor's* file logger (the task process doesn't run remote processors). On the first record for a log file it starts a `_LogStreamer` thread that tails the file and pushes complete lines every `stream_interval`, tracking the byte offset Loki accepted. `upload()` stops it and sends only the rest. It tails the file rather than rendering events so Loki lines are byte-identical to the file; the UI dedupes adjacent identical entries when merging worker and Loki logs for a running task. The task is identified from the path (`parse_log_path`, default `log_filename_template` only), since processors get no TI.
  - `processors` calls `LokiHook.cache_connection()`: the supervisor sets the remote-logging conn env var only while building processors, then removes it, so streamer threads would otherwise fail to resolve the connection.
  - `read` uses `ti.try_number`: the log API fetches the TI row for the requested try. `LokiTaskHandler._read_remote_logs` passes the requested try explicitly via `read_try`.
  - Connection: `get_hook` uses Airflow's `get_default_remote_conn_id()` (`remote_log_conn_id`, else `DEFAULT_REMOTE_CONN_ID`) because the supervisor pre-fetches exactly that connection into an env var before uploading.
- **Tests**: `tests/conftest.py` autouse fixtures set `AIRFLOW_CONN_LOKI_DEFAULT` and `AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID` env vars and configure the ORM; unit tests mock HTTP with `requests_mock`.
  - `test_loki_task_handler.py` and `integration/test_loki_roundtrip.py` use real DAG/DagRun/TaskInstance objects with Airflow 2-only APIs; `conftest.collect_ignore` skips them on Airflow 3.
  - `*_remote_log_io.py` modules and `log/test_loki_streaming.py` test `LokiRemoteLogIO` with a `SimpleNamespace` TI and run on both versions. Streaming tests fake the supervisor's logger as `SimpleNamespace(_file=<open file>)` and call `streamer.flush()` directly (large `stream_interval`) to stay deterministic.
  - Integration tests override `loki_conn_details` to point at `LOKI_URL`.
  - CI (`.github/workflows/ci.yml`) runs lint, unit (Airflow 2.11.2/3.4.0 × Py 3.11/3.12), and integration (both Airflow versions, Loki service container).

## User-facing setup (from README)

Both versions: copy Airflow's `airflow_local_settings.py` into a custom `log_config.py` on the `PYTHONPATH`, add a `remote_base_log_folder.startswith("loki")` branch, and set `remote_logging = True`, `remote_base_log_folder = loki`, `logging_config_class = log_config.DEFAULT_LOGGING_CONFIG` (and optionally `remote_log_conn_id`) in `airflow.cfg`. The template raises on unknown remote schemes, so the branch is required. Airflow 2: the branch installs `LokiTaskHandler` as the `task` handler. Airflow 3: the branch sets `REMOTE_TASK_LOG = LokiRemoteLogIO(...)`. With gevent workers (Airflow 2), reading logs can hit `RecursionError`; workaround is `gevent.monkey.patch_all()` at the top of `log_config.py`.
