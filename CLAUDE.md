# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Airflow provider package (`airflow-provider-grafana-loki`) that ships Airflow task logs to Grafana Loki and reads them back for the Airflow UI. Two components: `LokiHook` (HTTP client) and `LokiTaskHandler` (logging handler).

## Commands

```bash
# Known-working env: Python 3.11/3.12 + Airflow 2.11.2 (Airflow 3 is untested; its logging APIs differ)
uv venv .venv --python 3.12
uv pip install --python .venv -e ".[test]" "apache-airflow==2.11.2" apache-airflow-providers-http \
  --constraint https://raw.githubusercontent.com/apache/airflow/constraints-2.11.2/constraints-3.12.txt
airflow db migrate                # required once before tests: tests use the Airflow ORM (DagRun/TaskInstance)
                                  # set AIRFLOW_HOME to a throwaway dir to avoid touching ~/airflow
pytest                            # run all tests (integration tests skip unless LOKI_URL is set)
pytest tests/log/test_loki_task_handler.py::TestLokiHandler::test_read   # single test

# Integration tests against a real Loki (same as the CI `integration` job)
podman run -d --rm --name loki-it -p 3100:3100 docker.io/grafana/loki:3.4.2
LOKI_URL=http://localhost:3100 pytest tests/integration
flake8 grafana_loki_provider tests   # lint (max-line-length 100, configured in tox.ini)
```

Requires `apache-airflow-providers-http` at runtime (`LokiHook` subclasses `HttpHook`), though it is not declared in dependencies.

## Architecture

- **Provider registration**: `grafana_loki_provider/__init__.py:get_provider_info` is exposed via the `apache_airflow_provider` entry point. It registers the logging handler and the `grafana_loki` connection type. **The package version lives here** (`"versions": [...]`) — hatch reads it via regex in `pyproject.toml`. `setup.py` is legacy and has a stale version; `pyproject.toml` (hatchling) is the real build config.
- **`LokiHook`** (`hooks/loki.py`): thin `HttpHook` wrapper; `query_range` (GET) and `push_log` (POST, 3s timeout) against `/loki/api/v1/{method}`. Mutates `self.method` per call.
- **`LokiTaskHandler`** (`log/loki_task_handler.py`): extends Airflow's `FileTaskHandler`. Writes locally as normal; on `close()` reads the whole local log file and pushes it to Loki in one request (gzip-compressed JSON by default). Skips upload when `ti.raw` is true.
  - Stream **labels** (indexed): `dag_id`, `task_id`, `application="airflow"` — from `get_labels`.
  - **Extras** (`run_id`, `try_number`, `map_index`) are sent as the third element of each value tuple, i.e. Loki structured metadata, not stream labels — from `get_extras`. Values must be strings: Loki rejects the whole push with 400 otherwise.
  - ANSI escape sequences are stripped from each line before push (Loki doesn't support them).
  - `_read` builds a LogQL query: stream selector on `dag_id`/`task_id`, then `| label="..."` filters for the structured-metadata extras (they can't go in the `{...}` selector), queries a window of `start_date - 15d` to `end_date (or start+5d) + 1h`, limit 5000, and falls back to `FileTaskHandler._read` if Loki returns nothing.
  - Connection id comes from `[logging] remote_log_conn_id`.
- **Tests**: `tests/conftest.py` autouse fixtures set `AIRFLOW_CONN_LOKI_DEFAULT` and `AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID` env vars and configure the ORM; unit tests mock HTTP with `requests_mock`. `tests/integration/` overrides `loki_conn_details` to point at `LOKI_URL`, pushes via `close()` and reads back via `_read` with the local-file fallback patched to fail. CI (`.github/workflows/ci.yml`) runs lint, unit (Py 3.11/3.12), and integration (Loki service container).

## User-facing setup (from README)

Users enable the handler by copying Airflow's `airflow_local_settings.py` into a custom `log_config.py`, adding a branch for `REMOTE_BASE_LOG_FOLDER.startswith('loki')` that installs `grafana_loki_provider.log.loki_task_handler.LokiTaskHandler` as the `task` handler, and setting `remote_logging = True`, `remote_base_log_folder = loki`, `logging_config_class`, and `remote_log_conn_id` in `airflow.cfg`. With gevent workers, reading logs can hit `RecursionError`; workaround is `gevent.monkey.patch_all()` at the top of `log_config.py`.
