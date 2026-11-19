import pytest
from airflow import settings

from grafana_loki_provider.version_compat import AIRFLOW_V_3_0_PLUS

# These exercise the Airflow 2 upload-on-close path with real DAG/DagRun/TaskInstance objects,
# built with Airflow 2-only APIs. On Airflow 3, LokiRemoteLogIO is covered by the *_remote_log_io
# modules, which run on both versions.
collect_ignore = (
    ["log/test_loki_task_handler.py", "integration/test_loki_roundtrip.py"]
    if AIRFLOW_V_3_0_PLUS
    else []
)


@pytest.fixture(autouse=True, scope="module")
def session():
    settings.configure_orm()
    yield settings.Session


@pytest.fixture(scope='function')
def loki_conn_details():
    return dict(conn_type="grafana_loki",
                host="https://test.example.com",
                login="loki_username",
                password="loki_password",
                # conn_id = "loki_default"
                )


@pytest.fixture(scope='function')
def loki_urls(loki_conn_details):
    host = loki_conn_details['host']
    urls = {
        'query_range_url': host + "/loki/api/v1/query_range",
        'push_log_url': host + "/loki/api/v1/push"
    }
    return urls


@pytest.fixture(scope='function', autouse=True)
def init_loki_connection(mocker, loki_conn_details):
    import json
    conn_details = json.dumps(loki_conn_details)
    mocker.patch.dict("os.environ", AIRFLOW_CONN_LOKI_DEFAULT=conn_details)
    mocker.patch.dict("os.environ", AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID="lOKI_DEFAULT")
