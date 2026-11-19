[Join Slack Community](https://airflow-grafana-loki.slack.com/)

<p align="center">
  <a href="https://www.airflow.apache.org">
    <img alt="Airflow" src="https://cwiki.apache.org/confluence/download/attachments/145723561/airflow_transparent.png?api=v2" width="60" />
  </a>
</p>
<h1 align="center">
  Airflow Grafana Loki Provider
</h1>
  <h3 align="center">
  Log Handler for pushing Airflow Task Log to Grafana Loki
</h3>
<br/>

This package provides Hook and LogHandler that integrates with Grafana Loki. LokiTaskLogHandler is a python log handler that handles and reads task instance logs. It extends airflow FileTaskHandler and uploads to and reads from Grafana Loki.

Installation
--------------
Install version 0.2.1 from the GitHub release using `pip`:

```
pip install https://github.com/akurdyukov/airflow-provider-grafana-loki/releases/download/v0.2.1/airflow_provider_grafana_loki-0.2.1-py3-none-any.whl
```

Or install the tag straight from git:

```
pip install "git+https://github.com/akurdyukov/airflow-provider-grafana-loki.git@v0.2.1"
```

`apache-airflow-providers-http` is installed automatically as a dependency.

Note: `pip install airflow-provider-grafana-loki` from PyPI installs the older 0.0.2, which has neither the fixes from 0.0.3 nor Airflow 3 support.

Requirements:

- Airflow 2.x or 3.x (tested with 2.11.2 and 3.4.0 on Python 3.11 and 3.12). Airflow 3 needs version 0.1.0 or later; its setup differs, see [Airflow 3](#airflow-3) below.
- Loki 3.x, or Loki 2.9+ with structured metadata enabled. `run_id`, `try_number` and `map_index` are stored as structured metadata.

Logs pushed by versions before 0.0.3 can't be read back from Loki. For those runs, Airflow shows the local log file instead.

Configuration Airflow to write logs to Grafana Loki (Airflow 2)
---------------------------------------------------------------

Airflow can be configured to read and write task logs in Grafana Loki. It uses an existing
Airflow connection to read or write logs. If you don't have a connection properly setup,
this process will fail.

Follow the steps below to enable Grafana Loki logging:

1. Airflow's logging system requires a custom ``.py`` file to be located in the :envvar:`PYTHONPATH`, so that it's importable from Airflow. Start by creating a directory to store the config file, ``$AIRFLOW_HOME/config`` is recommended.
2. Create empty files called ``$AIRFLOW_HOME/config/log_config.py`` and ``$AIRFLOW_HOME/config/__init__.py``.
3. Copy the contents of ``airflow/config_templates/airflow_local_settings.py`` into the ``log_config.py`` file created in ``Step 2``.
4. Customize the following portions of the template:

```
     elif REMOTE_BASE_LOG_FOLDER.startswith('loki'):
        LOKI_HANDLER: Dict[str, Dict[str, Union[str, bool]]] = {
            'task': {
                'class': 'grafana_loki_provider.log.loki_task_handler.LokiTaskHandler',
                'formatter': 'airflow',
                'name':"airflow_task",
                'base_log_folder': str(os.path.expanduser(BASE_LOG_FOLDER)),
                'filename_template': FILENAME_TEMPLATE
            },
        }

        DEFAULT_LOGGING_CONFIG['handlers'].update(LOKI_HANDLER)
    else:
        raise AirflowException(
            "Incorrect remote log configuration. Please check the configuration of option 'host' in "
            "section 'elasticsearch' if you are using Elasticsearch. In the other case, "
            "'remote_base_log_folder' option in the 'logging' section."
        )




```

5. Make sure a Grafana Loki (Loki) connection hook has been defined in Airflow. The hook should have read and write access to the Grafana Loki Api.

6. Update ``$AIRFLOW_HOME/airflow.cfg`` to contain:

```

        [logging]
        remote_logging = True
        remote_base_log_folder = loki
        logging_config_class= log_config.DEFAULT_LOGGING_CONFIG
        remote_log_conn_id = <name of the Grafana Loki connection>
```

7. Restart the Airflow webserver and scheduler, and trigger (or wait for) a new task execution.
8. Verify that logs are showing up for newly executed tasks is showing up in Airflow UI. 

Airflow 3
---------------------------------------------------

In Airflow 3 the task supervisor uploads logs after each task finishes, and the API server reads them back. Both go through a `REMOTE_TASK_LOG` object defined in the logging config module, not through the task handler. The provider supplies `LokiRemoteLogIO` for this. The stock `FileTaskHandler` stays as the task handler.

1. Create ``$AIRFLOW_HOME/config/log_config.py`` (plus an empty ``__init__.py``) on the `PYTHONPATH`, as in steps 1-2 above.
2. Copy the contents of Airflow 3's ``airflow/config_templates/airflow_local_settings.py`` into it.
3. Add a `loki` branch before the final `else: raise AirflowException(...)`:

```python
    elif remote_base_log_folder.startswith("loki"):
        from grafana_loki_provider.log.loki_task_handler import LokiRemoteLogIO

        _default_conn_name_from("grafana_loki_provider.hooks.loki", "LokiHook")
        REMOTE_TASK_LOG = LokiRemoteLogIO(
            base_log_folder=BASE_LOG_FOLDER,
            delete_local_copy=delete_local_copy,
            **_io_kwargs,
        )
```

4. Update ``airflow.cfg``:

```
[logging]
remote_logging = True
remote_base_log_folder = loki
logging_config_class = log_config.DEFAULT_LOGGING_CONFIG
remote_log_conn_id = <name of the Grafana Loki connection>
```

`remote_log_conn_id` is optional; without it the `loki_default` connection is used. Extra `LokiRemoteLogIO` options can go in `remote_task_handler_kwargs`, for example `{"enable_gzip": false}`. `delete_local_logs = True` removes the local file after a successful upload.

### Streaming logs while the task runs

By default logs are uploaded when the task finishes; while a task runs, the UI shows its logs from the worker. To send lines to Loki as they're written (version 0.2.0 or later), set:

```
[logging]
remote_task_handler_kwargs = {"stream_logs": true}
```

The task supervisor then pushes new log lines every `stream_interval` seconds (default 2). When the task finishes, only the lines not yet sent are uploaded, so nothing is duplicated. If Loki is unreachable for a while, the unsent lines are retried on the next push or at task end.

Streaming identifies the task from the log file path, so it needs Airflow's default `log_filename_template` (`dag_id=…/run_id=…/task_id=…/attempt=N.log`). Logs with other paths are uploaded when the task finishes, as without streaming.

Notes:

- Each JSON log line is stored as-is with its own timestamp, so Airflow parses lines read back from Loki the same way as local log lines.
- Large logs are pushed in batches of up to 1000 lines or 1 MiB, under Loki's default request size limit.


in case you are using gevent worker class, you might face `RecursionError: maximum recursion depth exceeded` error while reading logs from Loki. 
please refer following issue for more info:[gevent/gevent/#1016]() [apache/airflow/#9118](https://github.com/apache/airflow/issues/9118)

current workaround is to add monkey patching at the top of the airflow log settings file. in this above case, ``$AIRFLOW_HOME/config/log_config.py``

eg:
```
""Airflow logging settings."""
from __future__ import annotations

import gevent.monkey
gevent.monkey.patch_all()
import os
```


Running tests
---------------------------------------------------
1. Run `airflow db migrate` to init database
2. Use `pytest` to run the tests

----

<h3> Note: The provider is in active  development stage. All sorts of feedback, and bug reports are welcome. I will try to addresss and resolve all issues to the best of my ability </h3> 
<h3> In case of any issue or you need any help, please feel free to open an issue. </h3>
<h3>Your contribution to the projects is highly appreciated and welcome.</h3>
