"""Anaplan Integration API client — metadata, bulk upload, and import."""

from __future__ import annotations

import time
from typing import Any

import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.models import (
    Action,
    Export,
    ExportTask,
    ImportAction,
    ImportDataSource,
    Model,
    Process,
    Workspace,
)
from anaplan_audit.exceptions import UnexpectedResponseError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def list_workspaces(client: APIClient, integration_uri: str) -> list[Workspace]:
    """List all workspaces visible to the authenticated user.

    ``?tenantDetails=true`` is always sent so the response includes the
    ``sizeAllowance`` and ``currentSize`` fields that the reporting model
    needs alongside id/name/active. The extra bytes are trivial (one row
    per workspace) and the flag is idempotent for tenants that don't
    grant tenant-details visibility — those fields simply come back as
    ``0``.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.

    Returns:
        A list of :class:`Workspace` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces", params={"tenantDetails": "true"})
    data = resp.json()
    return [Workspace.model_validate(w) for w in data.get("workspaces", [])]


def list_models(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
) -> list[Model]:
    """List all models in a workspace.

    ``?modelDetails=true`` is always sent so the response carries the
    ``lastModifiedByUserGuid`` / ``memoryUsage`` / ``lastSavedSerialNumber``
    / ``isoCreationDate`` / ``lastModified`` fields the reporting model's
    ``SYS Models`` module consumes. Without the flag Anaplan returns a
    minimal projection and every one of those columns lands blank —
    the exact regression the model-export-restoration spec targets.

    Note that ``currentSize`` and ``lastServerRestartDate`` are listed
    in the spec's Section 3.1 but Anaplan does *not* actually return
    them for models even with ``modelDetails=true`` (workspaces have
    ``currentSize``; models expose their size via ``memoryUsage``).
    The :class:`Model` class deliberately omits both.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.

    Returns:
        A list of :class:`Model` instances.
    """
    resp = client.get(
        f"{integration_uri}/workspaces/{workspace_id}/models",
        params={"modelDetails": "true"},
    )
    data = resp.json()
    return [Model.model_validate(m) for m in data.get("models", [])]


def list_actions(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[Action]:
    """List all actions in a model.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.

    Returns:
        A list of :class:`Action` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/actions")
    data = resp.json()
    return [Action.model_validate(a) for a in data.get("actions", [])]


def list_processes(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[Process]:
    """List all processes in a model.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.

    Returns:
        A list of :class:`Process` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/processes")
    data = resp.json()
    return [Process.model_validate(p) for p in data.get("processes", [])]


def list_files(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[ImportDataSource]:
    """List all files (data sources) in a model.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.

    Returns:
        A list of :class:`ImportDataSource` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/files")
    data = resp.json()
    return [ImportDataSource.model_validate(f) for f in data.get("files", [])]


def list_imports(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[ImportAction]:
    """List all import actions in a model.

    Used to resolve an import action *name* to its ID at runtime, so a
    settings file can reference imports by name — resilient to the IDs
    changing when a model is copied or rebuilt.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.

    Returns:
        A list of :class:`ImportAction` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/imports")
    data = resp.json()
    return [ImportAction.model_validate(i) for i in data.get("imports", [])]


def upload_file_chunks(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    file_id: str,
    data: str,
) -> None:
    """Upload data in chunks to an Anaplan file data source.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        file_id: Target file ID in the model.
        data: CSV-formatted string payload.
    """
    if not data:
        logger.warning("upload_skipped_empty_data", file_id=file_id)
        return

    chunk_size = 1_000_000
    chunks = [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]
    base = f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/files/{file_id}"

    client.post(base, json={"chunkCount": len(chunks)})

    for idx, chunk in enumerate(chunks):
        client.put(
            f"{base}/chunks/{idx}",
            data=chunk.encode(),
            headers={"Content-Type": "application/octet-stream"},
        )

    logger.info(
        "file_upload_complete",
        file_id=file_id,
        chunk_count=len(chunks),
        total_bytes=len(data),
    )


def list_exports(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[Export]:
    """List all export actions in a model.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.

    Returns:
        A list of :class:`Export` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/exports")
    data = resp.json()
    return [Export.model_validate(e) for e in data.get("exports", [])]


def trigger_export_task(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    export_id: str,
) -> str:
    """Trigger an export action and return the resulting task ID.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        export_id: ID of the export action to trigger.

    Returns:
        The task ID string for use with :func:`get_export_task_status`.

    Raises:
        UnexpectedResponseError: If the response does not contain a task ID.
    """
    resp = client.post(
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/exports/{export_id}/tasks",
        json={"localeName": "en_US"},
    )
    data = resp.json()
    task_id: str = data.get("task", {}).get("taskId", "")
    if not task_id:
        raise UnexpectedResponseError(
            "Export task response missing taskId",
            context={"export_id": export_id, "response": data},
        )
    logger.info("export_task_triggered", export_id=export_id, task_id=task_id)
    return task_id


def get_export_task_status(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    export_id: str,
    task_id: str,
) -> ExportTask:
    """Poll the status of a running export task.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        export_id: ID of the export action.
        task_id: Task ID returned by :func:`trigger_export_task`.

    Returns:
        An :class:`ExportTask` with the current ``taskState``.
    """
    resp = client.get(
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}"
        f"/exports/{export_id}/tasks/{task_id}"
    )
    data = resp.json()
    return ExportTask.model_validate(data.get("task", {}))


def download_export_file(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    export_id: str,
) -> str:
    """Download the completed export file as a raw CSV string.

    In Anaplan's Integration API v2, a completed export writes its output
    to a file whose ID matches the export action ID.  Anaplan splits large
    files into ~10 MB chunks — all chunks are downloaded in order and
    concatenated, so large model-history exports are never truncated.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        export_id: ID of the export action (also the output file ID).

    Returns:
        Raw CSV text content of the export file.
    """
    base = f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/files/{export_id}"

    resp = client.get(f"{base}/chunks")
    chunks = resp.json().get("chunks", [])
    if not chunks:
        # Older API behaviour / single-chunk files: fall back to chunk 0.
        return client.get(f"{base}/chunks/0").text

    parts: list[str] = []
    for chunk in chunks:
        chunk_id = chunk.get("id", "")
        parts.append(client.get(f"{base}/chunks/{chunk_id}").text)

    if len(parts) > 1:
        logger.info("export_file_multi_chunk_download", chunk_count=len(parts))
    return "".join(parts)


# Seconds between polls of a running import/process task.
_ACTION_POLL_INTERVAL: float = 5.0


def _summarize_nested_results(
    nested_results: list[dict[str, Any]],
    action_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Flatten a process task's ``nestedResults`` into a scannable log field.

    Each entry keeps the five things an operator needs to diagnose a
    process that reported ``successful=false``: the nested action's
    resolved name (falling back to *action_names* then the raw ID), the
    ID itself, whether it succeeded, whether it produced a failure dump,
    and the first ~2 lines of any localised error text.

    Args:
        nested_results: The raw ``result.nestedResults`` array from Anaplan.
        action_names: Optional mapping of action ID -> display name (usually
            built by the caller from ``list_imports`` on the target model).
            When provided, resolves entries whose ``objectName`` is missing
            or empty.
    """
    names = action_names or {}
    summary: list[dict[str, Any]] = []
    for n in nested_results:
        details = n.get("details", []) or []
        messages = [
            d.get("localMessageText", "")
            for d in details
            if isinstance(d, dict) and d.get("localMessageText")
        ][:2]
        object_id = n.get("objectId", "?")
        resolved_name = n.get("objectName") or names.get(object_id, "") or object_id
        summary.append(
            {
                "name": resolved_name,
                "id": object_id,
                "ok": bool(n.get("successful", True)),
                "failure_dump_available": bool(n.get("failureDumpAvailable", False)),
                "details": messages,
            }
        )
    return summary


def _run_action_task(
    client: APIClient,
    base_url: str,
    *,
    action_kind: str,
    action_id: str,
    timeout_seconds: int = 300,
    action_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Trigger an import/process task and poll it to completion.

    Anaplan action tasks "complete" even when the underlying import
    failed — the outcome lives in ``result.successful`` and per-file
    ``details``.  Both a FAILED task state and an unsuccessful result
    raise, so callers (and schedulers watching exit codes) learn about
    Anaplan-side failures instead of reporting success on a run that
    loaded zero rows.

    Args:
        client: An authenticated :class:`APIClient`.
        base_url: Action URL up to and including the action ID
            (e.g. ``…/models/M/imports/112000000041``).
        action_kind: ``"import"`` or ``"process"`` — used in logs/errors.
        action_id: The action ID (for logs/errors).
        timeout_seconds: Max seconds to wait for the task to finish.

    Returns:
        The terminal task dict (``taskState == "COMPLETE"``, successful).

    Raises:
        UnexpectedResponseError: If the task fails, the result is
            unsuccessful, or the timeout is reached.
    """
    resp = client.post(f"{base_url}/tasks", json={"localeName": "en_US"})
    task_id: str = resp.json().get("task", {}).get("taskId", "")
    if not task_id:
        # Some responses return taskId at the top level.
        task_id = resp.json().get("taskId", "")
    log = logger.bind(action_kind=action_kind, action_id=action_id, task_id=task_id)
    log.info(f"{action_kind}_started")

    if not task_id:
        # No task ID to poll — legacy response shape. Return what we got.
        log.warning(f"{action_kind}_task_id_missing", note="cannot poll for completion")
        return dict(resp.json())

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status_resp = client.get(f"{base_url}/tasks/{task_id}")
        task: dict[str, Any] = status_resp.json().get("task", {})
        state = task.get("taskState", "")

        if state == "COMPLETE":
            result = task.get("result", {})
            successful = result.get("successful", True)
            dump_available = result.get("failureDumpAvailable", False)
            details = result.get("details", []) or []
            nested_results = result.get("nestedResults", []) or []

            if not successful:
                nested_summary = _summarize_nested_results(nested_results, action_names)
                failed_nested_with_evidence = [
                    n
                    for n in nested_summary
                    if not n["ok"] and (n["failure_dump_available"] or n["details"])
                ]

                # For **processes**, Anaplan sets ``successful: false`` on any
                # non-perfect run — including harmless "rows ignored" from
                # nested imports that touched empty or partial data. Treat it
                # as a real failure whenever Anaplan actually points at
                # something: a top-level dump/details, OR any nested action
                # that reported its own dump/details. Import tasks (whose
                # ``successful`` signal is reliable) always fail hard.
                real_failure = (
                    action_kind != "process"
                    or dump_available
                    or bool(details)
                    or bool(failed_nested_with_evidence)
                )
                if real_failure:
                    log.error(
                        f"{action_kind}_failed_in_anaplan",
                        failure_dump_available=dump_available,
                        details=details,
                        nested_results=nested_summary,
                    )
                    raise UnexpectedResponseError(
                        f"Anaplan {action_kind} {action_id} completed unsuccessfully. "
                        "Check the failure dump in the Anaplan model.",
                        context={
                            "action_id": action_id,
                            "task_id": task_id,
                            "failure_dump_available": str(dump_available),
                            "failed_nested": ", ".join(
                                n["name"] for n in failed_nested_with_evidence
                            ),
                        },
                    )
                log.warning(
                    f"{action_kind}_completed_with_warnings",
                    nested_results=nested_summary,
                    note=(
                        "Anaplan reported successful=false with no failure "
                        "dump and no details. Common for processes whose "
                        "nested imports had rows ignored (e.g. empty inputs); "
                        "the data typically landed. Review the nested_results "
                        "above and the process's History in Anaplan to confirm."
                    ),
                )
                return task

            log.info(f"{action_kind}_complete", failure_dump_available=dump_available)
            return task

        if state in ("FAILED", "CANCELLED"):
            raise UnexpectedResponseError(
                f"Anaplan {action_kind} {action_id} task ended in state {state}.",
                context={"action_id": action_id, "task_id": task_id, "task_state": state},
            )

        time.sleep(_ACTION_POLL_INTERVAL)

    raise UnexpectedResponseError(
        f"Anaplan {action_kind} {action_id} did not complete within {timeout_seconds}s.",
        context={"action_id": action_id, "task_id": task_id},
    )


def run_process(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    process_id: str,
    *,
    action_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute an Anaplan process and poll it to completion.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        process_id: The process ID to execute.
        action_names: Optional mapping of nested action ID -> display
            name, used to resolve entries in the ``nested_results``
            log field when Anaplan omits ``objectName``.

    Returns:
        The terminal task dict.

    Raises:
        UnexpectedResponseError: If the process fails inside Anaplan or
            does not complete within the timeout.
    """
    base = f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/processes/{process_id}"
    return _run_action_task(
        client,
        base,
        action_kind="process",
        action_id=process_id,
        action_names=action_names,
    )


def run_import(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    import_id: str,
) -> dict[str, Any]:
    """Kick off an import action and poll it to completion.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        import_id: The import action ID.

    Returns:
        The terminal task dict.

    Raises:
        UnexpectedResponseError: If the import fails inside Anaplan or
            does not complete within the timeout.
    """
    base = f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/imports/{import_id}"
    return _run_action_task(client, base, action_kind="import", action_id=import_id)


def upload_and_import(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    file_id: str,
    import_id: str,
    data: str,
) -> None:
    """Upload a CSV payload and run its import action to completion.

    The shared sequence behind every "push data into Anaplan" call site.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        file_id: Target file ID in the model.
        import_id: Import action to run after the upload.
        data: CSV-formatted string payload.
    """
    upload_file_chunks(client, integration_uri, workspace_id, model_id, file_id, data)
    run_import(client, integration_uri, workspace_id, model_id, import_id)
