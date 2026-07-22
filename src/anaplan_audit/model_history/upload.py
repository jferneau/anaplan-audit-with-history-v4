"""Model History Upload — generate CSVs from DuckDB and push to Anaplan.

After model history data has been persisted to DuckDB, this module reads
the three model history tables, converts them to CSV, uploads them to the
target Anaplan model's pre-created data sources, and executes the
``"Load Model History"`` process.

Pre-requisites (set up once by the Anaplan model builder):
    - Data sources named exactly: ``MODEL_REGISTRY.csv``,
      ``MODEL_HISTORY_LIST.csv``, ``MODEL_HISTORY_NORMALIZED.csv``
    - A process named ``"Load Model History"``

Error handling:
    If any data source is not found or the process is missing, a clear
    warning is logged and the step is skipped — the audit run is not
    crashed.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.integration import (
    list_files,
    list_processes,
    run_process,
    upload_file_chunks,
)
from anaplan_audit.transform.loader import _connect

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

# Exact file names as they must appear in the Anaplan model.
_REGISTRY_FILE = "MODEL_REGISTRY.csv"
_LIST_FILE = "MODEL_HISTORY_LIST.csv"
_NORMALIZED_FILE = "MODEL_HISTORY_NORMALIZED.csv"

_LOAD_PROCESS = "Load Model History"

_SEPARATOR = "=" * 80

_TABLE_QUERIES: dict[str, str] = {
    _REGISTRY_FILE: "SELECT * FROM model_registry",
    _LIST_FILE: "SELECT * FROM model_history_list",
    _NORMALIZED_FILE: "SELECT * FROM model_history_normalized",
}


def upload_model_history(
    client: APIClient,
    db_path: Path,
    workspace_id: str,
    model_id: str,
    integration_uri: str,
    process_name: str = _LOAD_PROCESS,
) -> None:
    """Read model history from DuckDB and upload all three CSVs to Anaplan.

    Args:
        client: An authenticated :class:`~anaplan_audit.api.client.APIClient`.
        db_path: Path to the DuckDB database file.
        workspace_id: Anaplan workspace ID of the target model.
        model_id: Anaplan model ID of the target model.
        integration_uri: Base URI for the Integration API.
        process_name: Name of the Anaplan process to execute after upload.
            Defaults to ``"Load Model History"``.
    """
    log = logger.bind(
        workspace_id=workspace_id,
        model_id=model_id,
    )

    log.info("model_history_upload_section_start", separator=_SEPARATOR)

    # --- Resolve file IDs from the model ---
    files = list_files(client, integration_uri, workspace_id, model_id)
    file_map = {f.name: f.id for f in files}

    # --- Resolve process ID ---
    processes = list_processes(client, integration_uri, workspace_id, model_id)
    process = next((p for p in processes if p.name == process_name), None)

    # --- Upload each CSV ---
    all_uploaded = True
    with closing(_connect(db_path)) as conn:
        for file_name, query in _TABLE_QUERIES.items():
            file_id = file_map.get(file_name)
            if not file_id:
                log.error(
                    "model_history_data_source_not_found",
                    file_name=file_name,
                    instruction=(
                        f'Create a data source named "{file_name}" '
                        "in the target Anaplan model before running."
                    ),
                )
                all_uploaded = False
                continue

            df = conn.execute(query).df()
            csv_data = df.to_csv(index=False)
            row_count = len(df)

            log.info(
                "model_history_upload_starting",
                file_name=file_name,
                file_id=file_id,
                row_count=row_count,
            )

            upload_file_chunks(
                client,
                integration_uri,
                workspace_id,
                model_id,
                file_id,
                csv_data,
            )

            log.info(
                "model_history_upload_complete",
                file_name=file_name,
                row_count=row_count,
            )

    if not all_uploaded:
        log.warning(
            "model_history_upload_incomplete",
            note="One or more data sources were not found — skipping process execution.",
        )
        return

    # --- Execute the Load Model History process ---
    if process is None:
        log.error(
            "model_history_process_not_found",
            process_name=process_name,
            instruction=(
                f'Create a process named "{process_name}" '
                "in the target Anaplan model before running."
            ),
        )
        return

    log.info("model_history_process_starting", process_name=process_name, process_id=process.id)
    result = run_process(client, integration_uri, workspace_id, model_id, process.id)
    log.info(
        "model_history_process_complete",
        process_name=process_name,
        result=result,
    )
