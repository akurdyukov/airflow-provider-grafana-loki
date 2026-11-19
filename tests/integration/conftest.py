import os

import pytest


@pytest.fixture(scope="function")
def loki_conn_details():
    """Point the connection at a real Loki (overrides the mock host in tests/conftest.py)."""
    return dict(
        conn_type="grafana_loki",
        host=os.environ.get("LOKI_URL", "http://localhost:3100"),
    )
