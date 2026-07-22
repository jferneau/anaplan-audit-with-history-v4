"""Model History Service — trigger, poll, and download history exports.

For each model in scope, this module triggers the Anaplan model history
export action, polls until the task completes or times out, and returns
the raw CSV string for further processing.

Design constraints:
- Uses the existing authenticated APIClient — no new auth flows.
- All failures are logged as warnings; exceptions are never re-raised to
  the caller.  Model history failures must never crash the audit run.
- The export timeout is configurable (see :class:`ModelHistoryConfig`).
"""

from __future__ import annotations

import time

import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.integration import (
    download_export_file,
    get_export_task_status,
    list_exports,
    trigger_export_task,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

_POLL_INTERVAL: float = 10.0  # seconds between status checks


def fetch_model_history(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    workspace_name: str,
    model_id: str,
    model_name: str,
    export_action_name: str = "MODEL_HISTORY_EXPORT",
    timeout_seconds: int = 600,
) -> str | None:
    """Trigger a model history export, wait for completion, return CSV text.

    Args:
        client: An authenticated :class:`~anaplan_audit.api.client.APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        workspace_name: Human-readable workspace name (for logging).
        model_id: Anaplan model ID.
        model_name: Human-readable model name (for logging).
        export_action_name: Name of the export action in the model.
            Defaults to ``"MODEL_HISTORY_EXPORT"``.
        timeout_seconds: Maximum seconds to wait for the export task to
            complete.  Defaults to 600 (10 minutes).

    Returns:
        Raw CSV text of the export file, or ``None`` if the export action
        was not found, the task failed, or the timeout was reached.
    """
    log = logger.bind(
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        model_id=model_id,
        model_name=model_name,
    )

    # --- Locate the export action by name ---
    exports = list_exports(client, integration_uri, workspace_id, model_id)
    export = next((e for e in exports if e.name == export_action_name), None)

    if export is None:
        # Silently skip — not every model has a history export configured.
        return None

    export_id = export.id
    log = log.bind(export_id=export_id, export_action_name=export_action_name)
    log.info("model_history_export_triggered")

    # --- Trigger the export task ---
    task_id = trigger_export_task(client, integration_uri, workspace_id, model_id, export_id)
    log = log.bind(task_id=task_id)

    # --- Poll until COMPLETE, FAILED, or timeout ---
    deadline = time.monotonic() + timeout_seconds
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        elapsed = timeout_seconds - (deadline - time.monotonic())
        task = get_export_task_status(
            client, integration_uri, workspace_id, model_id, export_id, task_id
        )
        log.info(
            "model_history_export_poll",
            attempt=attempt,
            elapsed_seconds=round(elapsed, 1),
            task_state=task.taskState,
        )

        if task.taskState == "COMPLETE":
            break
        if task.taskState in ("FAILED", "CANCELLED"):
            log.warning(
                "model_history_export_failed",
                task_state=task.taskState,
            )
            return None

        time.sleep(_POLL_INTERVAL)
    else:
        log.warning(
            "model_history_export_timeout",
            timeout_seconds=timeout_seconds,
        )
        return None

    # --- Download the completed export file ---
    log.info("model_history_export_downloading")
    csv_text = download_export_file(client, integration_uri, workspace_id, model_id, export_id)

    # Count rows (excluding header) for logging.
    row_count = max(0, csv_text.count("\n") - 1)
    log.info("model_history_export_downloaded", row_count=row_count)

    return csv_text
