"""Top-level orchestrator — runs the full extract-transform-load pipeline."""

from __future__ import annotations

import contextlib
import importlib.resources
import os
import sys
import threading
import time

# Platform-specific run-lock primitive: fcntl.flock on POSIX,
# msvcrt.locking on Windows. mypy narrows on sys.platform, so each
# platform only type-checks its own branch.
if sys.platform == "win32":  # pragma: no cover — exercised on Windows CI
    import msvcrt
else:
    import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from io import StringIO
from pathlib import Path

import pandas as pd
import structlog
from pydantic import BaseModel

from anaplan_audit.api.audit import dump_audit_events, fetch_audit_events
from anaplan_audit.api.client import APIClient
from anaplan_audit.api.cloudworks import list_integrations
from anaplan_audit.api.integration import (
    list_actions,
    list_files,
    list_models,
    list_processes,
    list_workspaces,
)
from anaplan_audit.api.models import (
    Action,
    CloudWorksIntegration,
    ImportDataSource,
    Model,
    Process,
    User,
    Workspace,
)
from anaplan_audit.api.scim import list_users
from anaplan_audit.auth.basic import authenticate_basic
from anaplan_audit.auth.cert import authenticate_cert
from anaplan_audit.auth.models import AuthToken
from anaplan_audit.auth.oauth import refresh_access_token
from anaplan_audit.auth.token_store import TokenStore
from anaplan_audit.config import Settings, WorkspaceModelCombo
from anaplan_audit.exceptions import ConfigError, RunLockError
from anaplan_audit.model_history import classification
from anaplan_audit.model_history.history_service import fetch_model_history
from anaplan_audit.model_history.history_transform_service import normalize_model_history
from anaplan_audit.model_history.upload import upload_model_history
from anaplan_audit.transform.additional_attributes import enrich_event_dicts
from anaplan_audit.transform.loader import (
    _connect,
    _table_exists,
    backup_database,
    ensure_model_history_tables,
    ensure_staging_views,
    load_to_duckdb,
    purge_old_audit_events,
    purge_old_history,
    record_unmatched_descriptions,
    upsert_model_history,
)
from anaplan_audit.transform.runner import run_audit_query
from anaplan_audit.upload import upload_audit_data

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Run lock
# ---------------------------------------------------------------------------


class _RunLock:
    """Exclusive process-level lock backed by a ``*.lock`` file.

    Prevents two processes from running the pipeline against the same
    DuckDB database simultaneously.  (DuckDB itself also refuses a second
    writer process, but the run lock fails fast with a clear message
    instead of a mid-pipeline engine error.)  On Linux/macOS the lock is
    :func:`fcntl.flock`; on Windows it is :func:`msvcrt.locking` on the
    first byte of the lock file.  Either way the OS releases the lock if
    the process dies, and the lock is released explicitly when the
    context manager exits.

    Args:
        db_path: Path to the DuckDB database file.  The lock file is written
            alongside it with a ``.lock`` suffix.

    Raises:
        RunLockError: If the lock file is already held by another process.
    """

    def __init__(self, db_path: Path) -> None:
        self._lock_path = db_path.with_suffix(".lock")
        self._fd: int | None = None

    def __enter__(self) -> _RunLock:
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        try:
            if sys.platform == "win32":  # pragma: no cover — Windows CI
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # BlockingIOError (POSIX) and PermissionError (Windows) are
            # both OSError subclasses.
            os.close(self._fd)
            self._fd = None
            raise RunLockError(
                f"Another run is already in progress "
                f"(lock file: {self._lock_path}).  "
                "If no other run is active, delete the lock file and retry.",
                context={"lock_path": str(self._lock_path)},
            ) from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        if self._fd is not None:
            if sys.platform == "win32":  # pragma: no cover — Windows CI
                # Closing the fd releases the lock even if unlock fails.
                with contextlib.suppress(OSError):
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def run(
    settings: Settings,
    log: structlog.stdlib.BoundLogger,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> int:
    """Execute the pipeline according to the enabled feature flags.

    Behaviour is controlled by two settings:

    * ``auditEnabled`` (default ``True``) — runs Steps 2-6: metadata fetch,
      audit event fetch, DuckDB load, SQL transform, and Anaplan upload.
    * ``modelHistory.enabled`` (default ``False``) — runs Step 7: per-model
      history export, normalize, DuckDB upsert, Anaplan upload, and purge.

    Both flags can be ``true`` simultaneously (the default full-stack run).
    Setting ``auditEnabled = false`` with ``modelHistory.enabled = true``
    runs only the Model History pipeline — useful for back-filling history
    without touching audit data.

    The orchestrator holds an exclusive file lock for the duration of the
    run to prevent concurrent processes from contending for the shared
    DuckDB database.

    Args:
        settings: Validated application settings.
        log: A bound structlog logger.
        dry_run: When *True*, skip all Anaplan upload steps.

    Returns:
        ``0`` on success.

    Raises:
        RunLockError: If another run is already in progress.
        AnaplanAuditError: On any unrecoverable pipeline failure.
    """
    db_path = Path(settings.database)

    with _RunLock(db_path):
        return _run_locked(settings, log, db_path=db_path, dry_run=dry_run, limit=limit)


def _run_locked(
    settings: Settings,
    log: structlog.stdlib.BoundLogger,
    *,
    db_path: Path,
    dry_run: bool,
    limit: int | None = None,
) -> int:
    """Run the pipeline with the database lock already held."""
    # Step 1: Authenticate
    log.info("pipeline_step_start", step="authenticate")
    t0 = time.monotonic()
    token = _authenticate(settings)
    log.info("pipeline_step_done", step="authenticate", duration_ms=_elapsed(t0))

    # Build a token factory so the client can refresh mid-run.
    # The factory is protected by a module-level lock to serialize concurrent
    # refresh calls from the ThreadPoolExecutor workers.
    factory_lock = threading.Lock()

    def _token_factory() -> AuthToken:
        with factory_lock:
            return _authenticate(settings)

    # Metadata lookups shared between the audit and model history pipelines
    # so workspaces/models are listed exactly once per run.
    combos: list[WorkspaceModelCombo] | None = None
    ws_names: dict[str, str] = {}
    model_names: dict[str, str] = {}

    with APIClient(token, token_factory=_token_factory) as client:
        # Steps 2-6: Audit pipeline (skipped when auditEnabled = false)
        if settings.auditEnabled:
            # Step 2: Fetch metadata
            log.info("pipeline_step_start", step="fetch_metadata")
            t0 = time.monotonic()
            combos = _resolve_combos(client, settings)
            datasets, ws_names, model_names = _fetch_metadata(client, settings, combos)
            log.info("pipeline_step_done", step="fetch_metadata", duration_ms=_elapsed(t0))

            # Step 3: Fetch audit events
            log.info("pipeline_step_start", step="fetch_audit_events")
            t0 = time.monotonic()
            events = fetch_audit_events(
                client,
                settings.uris.auditUri,
                since_epoch=settings.lastRun,
                batch_size=settings.auditBatchSize,
                max_events=limit,
            )

            if len(events) == 0:
                log.warning(
                    "audit_api_returned_zero_events",
                    since_epoch=settings.lastRun,
                )
            else:
                # json_normalize flattens nested dicts (e.g. additionalAttributes)
                # into dotted column names that audit_query.sql references directly.
                # Only added when non-empty — pd.json_normalize([]) yields a 0x0
                # DataFrame with no columns, which would create a schema-less table
                # and break the unique index creation in _upsert_events.
                event_dumps = dump_audit_events(events)
                # v3.3.0 — enrich each event dump with the extracted
                # additionalAttributes columns (spec Milestone 1) before
                # json_normalize picks them up. Category gating and raw
                # retention come from settings; when the whole feature is
                # disabled, skip enrichment so the DataFrame stays a
                # verbatim projection of the API payload.
                aa_cfg = settings.additionalAttributes
                if aa_cfg.enabled:
                    enrich_event_dicts(
                        event_dumps,
                        enabled_categories=aa_cfg.enabled_category_names(),
                        retain_raw=aa_cfg.retainRawJson,
                        correlation_id=_bound_run_id(log),
                    )
                datasets["events"] = pd.json_normalize(event_dumps)

            log.info(
                "pipeline_step_done",
                step="fetch_audit_events",
                record_count=len(events),
                duration_ms=_elapsed(t0),
            )

            # Step 4: Load into DuckDB
            log.info("pipeline_step_start", step="load_duckdb")
            t0 = time.monotonic()
            load_to_duckdb(db_path, datasets)
            # v3.3.0 — refresh the additionalAttributes staging views
            # (spec Milestone 3). Idempotent and cheap; scoped to the
            # categories the operator has opted into via emitLists.
            if settings.additionalAttributes.enabled:
                ensure_staging_views(
                    db_path,
                    view_categories=settings.additionalAttributes.emit_list_categories(),
                )
            log.info("pipeline_step_done", step="load_duckdb", duration_ms=_elapsed(t0))

            # Step 5: Run SQL transform
            # Guard: on a first run where the API returned zero events the
            # events table will not exist yet.  Skip gracefully rather than
            # crashing — on subsequent runs the table is present from prior
            # loads, so historical data is still queried correctly.
            if not _has_events_table(db_path):
                log.warning("no_events_table_skipping_transform_and_upload")
            else:
                log.info("pipeline_step_start", step="sql_transform")
                t0 = time.monotonic()
                result_df = run_audit_query(db_path, tenant_name=settings.anaplanTenantName)
                log.info(
                    "pipeline_step_done",
                    step="sql_transform",
                    record_count=len(result_df),
                    duration_ms=_elapsed(t0),
                )

                # Step 6: Upload (unless dry-run or no rows to upload)
                if dry_run:
                    log.info("dry_run_skip_upload", row_count=len(result_df))
                elif len(result_df) == 0:
                    log.info("no_audit_rows_skipping_upload")
                else:
                    log.info("pipeline_step_start", step="upload")
                    t0 = time.monotonic()
                    upload_audit_data(client, result_df, settings, db_path=db_path)
                    log.info("pipeline_step_done", step="upload", duration_ms=_elapsed(t0))

            # Optional audit-event retention (0 = keep forever).
            if settings.auditRetentionYears > 0 and not dry_run:
                try:
                    backup_database(db_path, max_backups=settings.modelHistory.maxBackupsToKeep)
                    purge_old_audit_events(db_path, settings.auditRetentionYears)
                except Exception as exc:
                    log.warning("audit_retention_purge_error", error=str(exc))
        else:
            log.info("audit_disabled_skipping_steps_2_to_6")

        # Step 7: Model History (optional — failures never crash the audit run)
        mh_cfg = settings.modelHistory
        if mh_cfg.enabled and not dry_run:
            log.info("pipeline_step_start", step="model_history")
            t0 = time.monotonic()
            _run_model_history(
                client,
                settings,
                db_path,
                log,
                combos=combos,
                ws_names=ws_names,
                model_names=model_names,
            )
            log.info("pipeline_step_done", step="model_history", duration_ms=_elapsed(t0))
        elif mh_cfg.enabled and dry_run:
            log.info("dry_run_skip_model_history")
        else:
            log.debug("model_history_disabled")

    log.info("pipeline_complete")
    return 0


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _authenticate(settings: Settings) -> AuthToken:
    """Dispatch to the correct auth flow based on config.

    Args:
        settings: Application settings.

    Returns:
        A valid :class:`AuthToken`.

    Raises:
        AuthError: If authentication fails.
        ConfigError: If the auth mode is unknown.
    """
    mode = settings.authenticationMode

    if mode == "basic":
        if not settings.basic_username or not settings.basic_password:
            raise ConfigError(
                "Basic auth requires ANAPLAN_AUDIT_BASIC_USERNAME and "
                "ANAPLAN_AUDIT_BASIC_PASSWORD env vars.",
            )
        return authenticate_basic(
            settings.basic_username,
            settings.basic_password,
            settings.uris,
        )

    if mode == "cert_auth":
        public_path, private_path, passphrase = settings.resolved_cert_paths()
        return authenticate_cert(
            public_path,
            private_path,
            passphrase,
            settings.uris,
        )

    if mode == "OAuth":
        if not settings.oauthClientId:
            raise ConfigError(
                "OAuth mode requires oauthClientId in settings.json. "
                "Run 'anaplan-audit register --client-id <ID>' once — it "
                "stores the ID for you — or add the key manually.",
            )
        store = TokenStore()
        return refresh_access_token(
            settings.oauthClientId,
            settings.uris,
            store,
            rotatable=settings.rotatableToken,
        )

    raise ConfigError(f"Unknown authentication mode: {mode}")


# ---------------------------------------------------------------------------
# Metadata fetch
# ---------------------------------------------------------------------------


def _metadata_frame(
    rows: list[dict[str, object]],
    model_cls: type[BaseModel],
    *,
    extra: list[str] | None = None,
) -> pd.DataFrame:
    """Build a metadata DataFrame that always carries its expected columns.

    When *rows* is empty, ``pd.DataFrame([])`` has zero columns, which makes
    ``to_sql`` emit invalid ``CREATE TABLE t ()`` SQL and also strips the
    columns ``audit_query.sql`` joins on. This guarantees the Pydantic
    model's declared fields (plus any *extra* keys the orchestrator attaches)
    are present even for a 0-row result.

    Args:
        rows: The ``model_dump()`` dicts (possibly empty).
        model_cls: The Pydantic model whose field names define the columns.
        extra: Additional column names attached outside the model
            (e.g. ``workspaceId``, ``model_id``).

    Returns:
        A DataFrame with at least the expected columns.
    """
    columns = list(model_cls.model_fields.keys()) + list(extra or [])
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows)
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df


def _fetch_metadata(
    client: APIClient,
    settings: Settings,
    combos: list[WorkspaceModelCombo],
) -> tuple[dict[str, pd.DataFrame], dict[str, str], dict[str, str]]:
    """Fetch all metadata datasets plus name lookups.

    The workspace and model name lookups are returned so the model history
    pipeline can reuse them instead of re-listing the same workspaces and
    models (previously every metadata call happened twice on a full run).

    Args:
        client: An authenticated API client.
        settings: Application settings.
        combos: Workspace/model combos already resolved by the caller.

    Returns:
        A three-tuple ``(datasets, ws_names, model_names)`` where
        ``datasets`` maps table names to DataFrames and the two lookups
        map IDs to display names.
    """
    uri = settings.uris.integrationUri

    workspaces = list_workspaces(client, uri)
    workspaces_data = [w.model_dump() for w in workspaces]
    ws_names = {w.id: w.name for w in workspaces}

    users_data = [u.model_dump() for u in list_users(client, settings.uris.scimUri)]
    cloudworks_data = [
        c.model_dump() for c in list_integrations(client, settings.uris.cloudWorksUri)
    ]
    # v3.6.0 — the reporting model's SYS Cloudworks module has line
    # items named ``latestRun.triggeredBy``, ``schedule.name``, etc.
    # Anaplan matches those against dotted CSV column names, so flatten
    # the nested dicts here. ``pd.json_normalize`` on an empty list
    # produces a 0-row / 0-column frame, which _metadata_frame patches
    # by re-declaring every top-level field from CloudWorksIntegration.
    cloudworks_flat: list[dict[str, object]]
    if cloudworks_data:
        cloudworks_flat = [
            {str(k): v for k, v in row.items()}
            for row in pd.json_normalize(cloudworks_data).to_dict(orient="records")
        ]
    else:
        cloudworks_flat = []

    # Load activity_events.csv
    activity_csv = (
        importlib.resources.files("anaplan_audit.data").joinpath("activity_events.csv").read_text()
    )
    activity_df = pd.read_csv(StringIO(activity_csv))

    all_models: list[dict[str, object]] = []
    all_actions: list[dict[str, object]] = []
    all_processes: list[dict[str, object]] = []
    all_files: list[dict[str, object]] = []
    model_names: dict[str, str] = {}

    # The (workspace, model) pairs actually in scope. Action/process metadata
    # is fetched ONLY for these — so `select` genuinely limits which models
    # the audit path touches, and a model that isn't selected (archived,
    # inaccessible, being copied) is never queried and can't 404 the run.
    selected_pairs = {(c.workspaceId, c.modelId) for c in combos}

    # List models across EVERY workspace in the tenant, not just the selected
    # ones, so the MODEL / WORKSPACE lookup tables are complete. Audit events
    # are tenant-wide and can reference a model in any workspace (directly via
    # additionalAttributes.modelId, or via objectId for action-execution
    # events), so a model list scoped to selected workspaces leaves those
    # events unattributed. Listing models is one cheap, non-404-prone call per
    # workspace; the expensive, per-model, 404-prone actions/processes/files
    # calls stay gated on selected_pairs below. (Matches v1 behaviour.)
    all_workspace_ids = list(dict.fromkeys(w.id for w in workspaces))

    for ws_id in all_workspace_ids:
        try:
            models = list_models(client, uri, ws_id)
        except Exception as exc:
            # A whole workspace being unreachable shouldn't halt the run.
            logger.warning("metadata_list_models_failed", workspace_id=ws_id, error=str(exc))
            continue

        for m in models:
            # Model names/rows are cheap and improve name resolution in the
            # report, so keep every model in the workspace's lookup tables.
            model_names[m.id] = m.name
            m_dict = m.model_dump()
            # Quinn's v1 (anaplan_ops.py) drops exactly this column and
            # keeps every other detail field. categoryValues is a nested
            # dict Anaplan uses internally for the model-hub UI; it has
            # no downstream Anaplan Reporting Model consumer and would
            # otherwise land as an unreadable json-serialised blob in
            # MODEL_LIST.csv. Deliberate parity with Quinn per spec Section 2.
            m_dict.pop("categoryValues", None)
            m_dict["workspaceId"] = ws_id
            all_models.append(m_dict)

            # Actions/processes are the expensive, per-model, 404-prone calls.
            # Only fetch them for models that are actually selected.
            if (ws_id, m.id) not in selected_pairs:
                continue

            try:
                # Every metadata table attaches its provenance columns in
                # snake_case — workspace_id / model_id / workspace_name /
                # model_name — so SYS Files / SYS Actions / SYS Processes share
                # one consistent import contract that matches the model_history
                # tables. Names are resolved here (the lookups are already in
                # hand) so the reporting modules can show them directly instead
                # of a FINDITEM formula that only resolves when the model's
                # MODEL / WORKSPACE lists happen to contain the id.
                actions = list_actions(client, uri, ws_id, m.id)
                for a in actions:
                    a_dict = a.model_dump()
                    a_dict["workspace_id"] = ws_id
                    a_dict["model_id"] = m.id  # SQL: a.id || a.model_id
                    a_dict["workspace_name"] = ws_names.get(ws_id, "")
                    a_dict["model_name"] = m.name
                    all_actions.append(a_dict)

                processes = list_processes(client, uri, ws_id, m.id)
                for p in processes:
                    p_dict = p.model_dump()
                    p_dict["workspace_id"] = ws_id
                    p_dict["model_id"] = m.id
                    p_dict["workspace_name"] = ws_names.get(ws_id, "")
                    p_dict["model_name"] = m.name
                    all_processes.append(p_dict)

                # v3.5.0 — files feed the reporting model's SYS Files
                # module. Same 404-risk profile as actions (a selected
                # model that's archived / read-only can 404), so it
                # shares the same try/except.
                files = list_files(client, uri, ws_id, m.id)
                for f in files:
                    f_dict = f.model_dump()
                    f_dict["workspace_id"] = ws_id
                    f_dict["model_id"] = m.id
                    f_dict["workspace_name"] = ws_names.get(ws_id, "")
                    f_dict["model_name"] = m.name
                    all_files.append(f_dict)
            except Exception as exc:
                # A single selected-but-inaccessible model is logged and
                # skipped rather than crashing the whole audit run.
                logger.warning(
                    "metadata_model_actions_skipped",
                    workspace_id=ws_id,
                    model_id=m.id,
                    error=str(exc),
                )
                continue

    # Build each metadata frame with guaranteed columns, so an empty result
    # (e.g. a tenant with no CloudWorks integrations, or a model with no
    # actions) still produces a properly-columned 0-row table. Without this,
    # pd.DataFrame([]) has no columns and to_sql emits "CREATE TABLE t ()" —
    # an "near ')': syntax error" — and audit_query.sql's joins would break.
    datasets = {
        "workspaces": _metadata_frame(workspaces_data, Workspace),
        "users": _metadata_frame(users_data, User),
        "cloudworks": _metadata_frame(cloudworks_flat, CloudWorksIntegration),  # SQL: cloudworks cw
        "models": _metadata_frame(all_models, Model, extra=["workspaceId"]),
        "actions": _metadata_frame(
            all_actions,
            Action,
            extra=["workspace_id", "model_id", "workspace_name", "model_name"],
        ),
        "processes": _metadata_frame(
            all_processes,
            Process,
            extra=["workspace_id", "model_id", "workspace_name", "model_name"],
        ),
        # v3.5.0 — ``files`` is a fresh dataset. ImportDataSource declares
        # ``id`` + ``name``; the four provenance columns (snake_case) are
        # attached in the loop above.
        "files": _metadata_frame(
            all_files,
            ImportDataSource,
            extra=["workspace_id", "model_id", "workspace_name", "model_name"],
        ),
        "act_codes": activity_df,  # SQL: act_codes ac
    }

    # Drop always-empty CloudWorks columns so CLOUDWORKS_LIST.csv carries no
    # dead fields: `type`/`uxVisible` are erroneous model declarations the API
    # never fills (it returns `integrationType`/`nuxVisible`, which are kept),
    # and `latestRun`/`schedule` are the raw nested dicts that json_normalize
    # already flattened into `latestRun.*`/`schedule.*` — the raw columns only
    # get re-added (empty) by _metadata_frame's column guarantee.
    datasets["cloudworks"] = datasets["cloudworks"].drop(
        columns=["type", "uxVisible", "latestRun", "schedule"], errors="ignore"
    )
    return datasets, ws_names, model_names


def _resolve_combos(
    client: APIClient,
    settings: Settings,
) -> list[WorkspaceModelCombo]:
    """Resolve workspace/model combos based on filter approach.

    In ``select`` mode, each combo may reference the workspace and model by
    **ID or display name** — names are resolved to IDs against the live
    tenant, so customers don't need to dig IDs out of URLs.

    Args:
        client: An authenticated API client.
        settings: Application settings.

    Returns:
        The list of workspace/model combos to process (always IDs).

    Raises:
        ConfigError: If a workspace or model name/ID cannot be resolved.
    """
    if settings.workspaceModelFilterApproach == "select":
        return _resolve_names_to_ids(client, settings, settings.workspaceModelCombos)

    # "skip" mode — get all workspaces, exclude the listed combos
    skip_set = {(c.workspaceId, c.modelId) for c in settings.workspaceModelCombos}
    uri = settings.uris.integrationUri
    result: list[WorkspaceModelCombo] = []

    for ws in list_workspaces(client, uri):
        for m in list_models(client, uri, ws.id):
            if (ws.id, m.id) not in skip_set:
                result.append(WorkspaceModelCombo(workspaceId=ws.id, modelId=m.id))

    return result


def _resolve_names_to_ids(
    client: APIClient,
    settings: Settings,
    combos: list[WorkspaceModelCombo],
) -> list[WorkspaceModelCombo]:
    """Translate name-based combos to ID-based combos.

    Each combo value is first checked against known IDs; anything that
    isn't an ID is looked up as a display name (exact match first, then
    case-insensitive).

    Args:
        client: An authenticated API client.
        settings: Application settings.
        combos: Combos as configured — IDs, names, or a mix.

    Returns:
        Combos with both fields guaranteed to be IDs.

    Raises:
        ConfigError: If any workspace or model cannot be resolved.
    """
    if not combos:
        return combos

    uri = settings.uris.integrationUri
    workspaces = list_workspaces(client, uri)
    ws_ids = {w.id for w in workspaces}
    ws_by_name = {w.name: w.id for w in workspaces}
    ws_by_name_ci = {w.name.lower(): w.id for w in workspaces}

    resolved: list[WorkspaceModelCombo] = []
    for combo in combos:
        ws_ref = combo.workspaceId
        if ws_ref in ws_ids:
            ws_id = ws_ref
        else:
            maybe = ws_by_name.get(ws_ref) or ws_by_name_ci.get(ws_ref.lower())
            if maybe is None:
                raise ConfigError(
                    f"Workspace '{ws_ref}' not found by ID or name.",
                    context={"workspace": ws_ref},
                )
            ws_id = maybe
            logger.info("workspace_name_resolved", name=ws_ref, workspace_id=ws_id)

        models = list_models(client, uri, ws_id)
        model_ids = {m.id for m in models}
        m_by_name = {m.name: m.id for m in models}
        m_by_name_ci = {m.name.lower(): m.id for m in models}

        m_ref = combo.modelId
        if m_ref in model_ids:
            m_id = m_ref
        else:
            maybe = m_by_name.get(m_ref) or m_by_name_ci.get(m_ref.lower())
            if maybe is None:
                raise ConfigError(
                    f"Model '{m_ref}' not found by ID or name in workspace {ws_id}.",
                    context={"model": m_ref, "workspace_id": ws_id},
                )
            m_id = maybe
            logger.info("model_name_resolved", name=m_ref, model_id=m_id)

        resolved.append(WorkspaceModelCombo(workspaceId=ws_id, modelId=m_id))

    return resolved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _elapsed(start: float) -> int:
    """Return elapsed milliseconds since *start*."""
    return round((time.monotonic() - start) * 1000)


def _bound_run_id(log: structlog.stdlib.BoundLogger) -> str | None:
    """Pull the structlog-bound ``run_id`` off the logger, if any.

    structlog's stdlib BoundLogger keeps bound context on a private
    attribute; the extractor wants the run_id for correlation without
    a hard dependency on the logger internals. If the attribute is
    absent (mocked logger, tests) we degrade to ``None`` — the
    extractor treats that as "no correlation".
    """
    context = getattr(log, "_context", {}) or {}
    return context.get("run_id") if isinstance(context, dict) else None


def _has_events_table(db_path: Path) -> bool:
    """Return ``True`` if the ``events`` table exists in the DuckDB database.

    Used to guard the SQL transform step on first runs where the audit API
    returns zero events — the table is never created in that case, and
    ``audit_query.sql`` would fail with a missing-table error.
    """
    if not db_path.exists():
        return False
    with closing(_connect(db_path)) as conn:
        return _table_exists(conn, "events")


# ---------------------------------------------------------------------------
# Model History
# ---------------------------------------------------------------------------


def _run_model_history(
    client: APIClient,
    settings: Settings,
    db_path: Path,
    log: structlog.stdlib.BoundLogger,
    *,
    combos: list[WorkspaceModelCombo] | None = None,
    ws_names: dict[str, str] | None = None,
    model_names: dict[str, str] | None = None,
) -> None:
    """Run the full model history extract-transform-load sequence.

    Architecture
    ~~~~~~~~~~~~
    Exports are fetched and normalized in parallel using a
    :class:`~concurrent.futures.ThreadPoolExecutor` with up to
    ``modelHistory.maxConcurrentExports`` workers.  The underlying
    :class:`~anaplan_audit.api.client.APIClient` is safe to share across
    threads (:class:`httpx.Client` is thread-safe).

    All database writes are performed serially on the main thread after
    every worker completes.  PRESERVED INVARIANT — do not "optimize" this
    by having workers upsert their own results: worker threads only do
    HTTP fetch + in-memory normalization by design.  DuckDB's concurrent
    write handling (optimistic concurrency, transaction conflicts) is
    less forgiving than SQLite's was, so parallel writers would introduce
    real contention that this structure deliberately avoids.

    All exceptions are caught and logged as warnings — model history
    failures must never crash the audit run.

    Args:
        client: An authenticated API client (shared across worker threads).
        settings: Application settings.
        db_path: Path to the DuckDB database file.
        log: A bound structlog logger.
    """
    mh_cfg = settings.modelHistory
    uri = settings.uris.integrationUri
    # Model history uploads to its OWN target model, distinct from the audit
    # reporting model (settings.targetAnaplanModel). The config validator
    # guarantees both fields are set whenever modelHistory is enabled.
    target = mh_cfg.targetAnaplanModel

    # Ensure the model-history schema exists.
    try:
        ensure_model_history_tables(db_path)
    except Exception as exc:
        log.warning("model_history_schema_error", error=str(exc))
        return

    if combos is None:
        combos = _resolve_combos(client, settings)

    # Reuse lookups from the audit metadata fetch when available; only
    # re-list when the audit pipeline was disabled this run.
    if not ws_names:
        try:
            workspaces = list_workspaces(client, uri)
            ws_names = {w.id: w.name for w in workspaces}
        except Exception as exc:
            log.warning("model_history_workspace_lookup_error", error=str(exc))
            ws_names = {}

    if not model_names:
        model_names = {}
        for ws_id in dict.fromkeys(c.workspaceId for c in combos):
            try:
                for m in list_models(client, uri, ws_id):
                    model_names[m.id] = m.name
            except Exception as exc:
                log.warning(
                    "model_history_model_lookup_error",
                    workspace_id=ws_id,
                    error=str(exc),
                )

    # --- Parallel export + normalize ---
    # Collect successful normalized results and per-model errors separately.
    # The results list and errors list are written from worker threads but
    # only read on the main thread — a simple lock is sufficient.
    results: list[tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = []
    errors: list[tuple[str, str, str]] = []
    results_lock = threading.Lock()

    def _process_combo(combo: WorkspaceModelCombo) -> None:
        ws_id = combo.workspaceId
        m_id = combo.modelId
        ws_name = ws_names.get(ws_id, ws_id)
        m_name = model_names.get(m_id, m_id)

        try:
            csv_text = fetch_model_history(
                client=client,
                integration_uri=uri,
                workspace_id=ws_id,
                workspace_name=ws_name,
                model_id=m_id,
                model_name=m_name,
                export_action_name=mh_cfg.exportActionName,
                timeout_seconds=mh_cfg.exportTimeoutSeconds,
            )
            if csv_text is None:
                return

            registry_df, list_df, norm_df = normalize_model_history(
                csv_text=csv_text,
                model_id=m_id,
                model_name=m_name,
                workspace_id=ws_id,
                workspace_name=ws_name,
            )

            with results_lock:
                results.append((registry_df, list_df, norm_df))

        except Exception as exc:
            with results_lock:
                errors.append((ws_id, m_id, str(exc)))

    max_workers = mh_cfg.maxConcurrentExports
    log.info(
        "model_history_export_start",
        combo_count=len(combos),
        max_concurrent=max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process_combo, combo): combo for combo in combos}
        for future in as_completed(futures):
            # Exceptions inside _process_combo are caught; result() is a no-op.
            future.result()

    # Log per-model errors collected by workers.
    for ws_id, m_id, err in errors:
        log.warning(
            "model_history_model_error",
            workspace_id=ws_id,
            model_id=m_id,
            error=err,
        )

    # --- Serial DuckDB upserts (preserved invariant — see docstring) ---
    for registry_df, list_df, norm_df in results:
        try:
            upsert_model_history(db_path, registry_df, list_df, norm_df)
        except Exception as exc:
            log.warning("model_history_upsert_error", error=str(exc))

    # --- Failsafe: record descriptions that no change-type rule matched,
    # aggregated across every model this run, so colleagues have a durable
    # working set for authoring rules (see `anaplan-audit mh-unmatched`).
    try:
        unmatched: dict[str, int] = {}
        for _registry_df, _list_df, norm_df in results:
            for description, occ in classification.unmatched_counts(
                norm_df["description"], norm_df["change_type"]
            ).items():
                unmatched[description] = unmatched.get(description, 0) + occ
        record_unmatched_descriptions(db_path, unmatched)
    except Exception as exc:
        log.warning("model_history_unmatched_record_error", error=str(exc))

    # --- Upload all accumulated history to Anaplan ---
    try:
        upload_model_history(
            client=client,
            db_path=db_path,
            workspace_id=target.workspaceId,
            model_id=target.modelId,
            integration_uri=uri,
            process_name=mh_cfg.anaplanProcess,
        )
    except Exception as exc:
        log.warning("model_history_upload_error", error=str(exc))

    # --- Backup then purge records beyond retention window ---
    if mh_cfg.backupBeforePurge:
        try:
            backup_database(db_path, max_backups=mh_cfg.maxBackupsToKeep)
        except Exception as exc:
            log.warning("model_history_backup_error", error=str(exc))

    try:
        purge_old_history(db_path, retention_years=mh_cfg.retentionYears)
    except Exception as exc:
        log.warning("model_history_purge_error", error=str(exc))


# Keep a module-level reference so tests can import the lock class directly.
RunLock = _RunLock
