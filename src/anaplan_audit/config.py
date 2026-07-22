"""Application configuration via pydantic-settings.

Supports layered precedence: CLI flag > env var (``ANAPLAN_AUDIT_`` prefix) >
``.env`` > ``settings.json`` > defaults.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from anaplan_audit.exceptions import ConfigError


def split_cert_path_and_passphrase(raw: str) -> tuple[str, str | None]:
    """Split an optional ``:passphrase`` suffix from a certificate path.

    The legacy convention encodes the private-key passphrase inline as
    ``path:passphrase``.  A naive ``split(":")`` breaks Windows drive-letter
    paths (``C:\\certs\\key.pem`` → ``"C"``), so the drive prefix is peeled
    off before looking for the passphrase separator.  Works on both POSIX
    and Windows:

    - ``C:\\certs\\key.pem``          → (``C:\\certs\\key.pem``, ``None``)
    - ``C:\\certs\\key.pem:secret``   → (``C:\\certs\\key.pem``, ``"secret"``)
    - ``/etc/anaplan/key.pem``       → (``/etc/anaplan/key.pem``, ``None``)
    - ``/etc/anaplan/key.pem:secret`` → (``/etc/anaplan/key.pem``, ``"secret"``)

    Prefer the dedicated ``certPassphrase`` setting over the inline form.

    Args:
        raw: The configured path, optionally with a ``:passphrase`` suffix.

    Returns:
        ``(path, passphrase_or_None)``.
    """
    drive = ""
    rest = raw
    # Windows drive-letter prefix: "C:\" or "C:/".
    if len(raw) >= 3 and raw[0].isalpha() and raw[1] == ":" and raw[2] in ("\\", "/"):
        drive, rest = raw[:2], raw[2:]
    if ":" in rest:
        path_part, passphrase = rest.rsplit(":", 1)
        return drive + path_part, passphrase
    return raw, None


class ListSyncEntry(BaseModel):
    """One list in the target model whose codes we keep in sync.

    On each successful run the tool takes the distinct values in
    ``codeColumn`` from the transformed audit DataFrame, diffs them
    against the list's existing codes via the Transactional API, and
    POSTs any net-new codes as list items. Failures are logged as
    warnings and never crash the run.
    """

    model_config = ConfigDict(populate_by_name=True)

    listName: str
    """Name of the target list in the reporting model."""

    codeColumn: str
    """Column in the transformed audit DataFrame whose distinct values
    are treated as list codes. Common values: ``EVENT_ID`` (event
    types), ``AUDIT_ID`` (per-event unique IDs)."""


class WorkspaceModelCombo(BaseModel):
    """A workspace/model pair for filtering."""

    model_config = ConfigDict(populate_by_name=True)

    workspaceId: str
    modelId: str


class AnaplanUris(BaseModel):
    """Base URLs for each Anaplan API surface."""

    model_config = ConfigDict(populate_by_name=True)

    authServiceUri: str = "https://auth.anaplan.com/token/authenticate"
    authTokenVerify: str = "https://auth.anaplan.com/token/validate"
    oauthServiceUri: str = "https://us1a.app.anaplan.com/oauth"
    integrationUri: str = "https://api.anaplan.com/2/0"
    auditUri: str = "https://audit.anaplan.com/audit/api/1"
    scimUri: str = "https://api.anaplan.com/scim/1/0/v2"
    cloudWorksUri: str = "https://api.cloudworks.anaplan.com/2/0"


class TargetModelObjects(BaseModel):
    """File and import references within the target Audit Reporting Model.

    Two upload architectures are supported:

    * **Multi-file + process (v1-compatible).** Set ``processName`` to run
      an Anaplan process that ingests eight per-table CSVs (audit events,
      users, workspaces, models, actions, files, cloudworks, activity
      codes) uploaded to their named file sources. This matches the v1
      reporting-model design. The CSV *file* names in the target model are
      configured via the ``*FileName`` fields; each defaults to what v1
      used, so most customers set only ``processName``.
    * **Single-file (v3 default).** Set ``auditFileName`` +
      ``auditImportName``. The tool runs its local SQL transform, uploads
      a single blended CSV, and runs one import action.

    Prefer names over IDs: they are resolved to IDs at runtime, so a
    model copy or rebuild (which changes the numeric IDs) does not break
    the configuration. The ``*Id`` fields remain as an explicit override.
    """

    model_config = ConfigDict(populate_by_name=True)

    # --- Multi-file + process mode (v1-compatible) ---------------------
    processName: str = ""
    """Name of the Anaplan process to run after uploading the per-table
    CSVs. When set, the tool switches to multi-file + process mode.

    The v1 process was ``"Update Anaplan Audit Environment"``.
    """

    # Per-table CSV file names in the target reporting model. Defaults
    # match what v1 shipped, so most users only need to set ``processName``.
    auditEventsFileName: str = "AUDIT_LOG.csv"
    usersFileName: str = "USER_LIST.csv"
    workspacesFileName: str = "WORKSPACE_LIST.csv"
    modelsFileName: str = "MODEL_LIST.csv"
    actionsFileName: str = "ACTION_LIST.csv"
    filesFileName: str = "FILE_LIST.csv"
    cloudworksFileName: str = "CLOUDWORKS_LIST.csv"
    activityCodesFileName: str = "ACTIVITY_CODES.csv"

    # --- Single-file mode (v3 default) ----------------------------------
    # Blended CSV produced by audit_query.sql -> one file, one import.
    auditFileName: str = ""
    auditImportName: str = ""

    # --- Optional last-run display object -------------------------------
    lastRunFileName: str = ""
    lastRunImportName: str = ""

    # --- Refresh Log via Transactional API ------------------------------
    # When both list and module are set, the tool appends a run entry to
    # the list (code = epoch seconds) and writes the two named line-item
    # cells for that batch — no file/import mapping needed inside the
    # target model. Leave any blank to disable the refresh-log path.
    batchIdListName: str = ""
    """Name of the BATCH_ID list. A new item is added on each successful
    run with epoch seconds as its Code, before the refresh log cells are
    written. Leave blank to skip the transactional refresh-log path."""

    refreshLogModuleName: str = ""
    """Name of the module receiving the refresh-log cells."""

    refreshLogTimeStampLineItem: str = "Time Stamp"
    """Line-item name in the refresh log module that receives the ISO
    UTC timestamp of this run (as a string)."""

    refreshLogRecordsLoadedLineItem: str = "Audit Records Loaded"
    """Line-item name in the refresh log module that receives the count
    of audit rows loaded on this run (as an integer)."""

    # --- additionalAttributes staging-view CSV uploads (v3.7.0) --------
    # Each view is emitted as a two-column ``(code, name)`` CSV and
    # uploaded to the named Anaplan file source, ready for a list
    # import. Leave any file-name blank to skip that view — belt with
    # the ``additionalAttributes.categories.<X>.emitLists`` setting,
    # which also determines whether the view is materialised at all.
    uxAppListFileName: str = ""
    uxPageListFileName: str = ""
    cwIntegrationListFileName: str = ""
    actionListFileName: str = ""
    processListFileName: str = ""
    roleListFileName: str = ""
    targetUserListFileName: str = ""

    # --- List sync via Transactional API --------------------------------
    syncLists: list[ListSyncEntry] = []
    """Lists whose codes should be kept current with what this run
    observed. For each entry the tool diffs the distinct
    ``codeColumn`` values in the transformed audit DataFrame against
    the list's existing codes and POSTs any net-new codes as list
    items. Belt-and-suspenders for reporting models that also load
    these lists via nested imports."""

    # --- Legacy / explicit-override IDs ---------------------------------
    auditFileId: str = ""
    auditImportId: str = ""
    lastRunFileId: str = ""
    lastRunImportId: str = ""


class TargetModelConfig(BaseModel):
    """Target Anaplan model for the upload step."""

    model_config = ConfigDict(populate_by_name=True)

    workspaceId: str = ""
    modelId: str = ""
    objects: TargetModelObjects = TargetModelObjects()


class AdditionalAttributesCategoryConfig(BaseModel):
    """Per-category gate for the additionalAttributes feature.

    ``enabled`` controls whether the category's named columns are
    populated by the extractor. ``emitLists`` controls whether the
    matching staging view (v_ux_app, v_ux_page, v_action, …) is
    materialised — a caller might want the columns populated for local
    querying but not exported for Anaplan list import.
    """

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = True
    emitLists: bool = True


class AdditionalAttributesConfig(BaseModel):
    """Top-level config for the additionalAttributes extractor (v3.3.0).

    Mirrors the ``modelHistory`` block's shape so operators reason
    about the two features the same way. The default matches the
    canonical block in the spec Milestone 5.
    """

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = True
    """When ``False``, skip parsing entirely — no named columns are
    populated on new events, no staging views are refreshed. Existing
    data on disk is unaffected."""

    retainRawJson: bool = True
    """When ``True``, ``additional_attributes_raw`` is populated with a
    stable JSON serialization of the parsed dict. Preserves forward
    compatibility for fields the Section 4 map doesn't cover today."""

    categories: dict[str, AdditionalAttributesCategoryConfig] = {
        "uxAppPage": AdditionalAttributesCategoryConfig(enabled=True, emitLists=True),
        "cwIntegration": AdditionalAttributesCategoryConfig(enabled=True, emitLists=False),
        "action": AdditionalAttributesCategoryConfig(enabled=True, emitLists=False),
        "process": AdditionalAttributesCategoryConfig(enabled=True, emitLists=False),
        "role": AdditionalAttributesCategoryConfig(enabled=False, emitLists=False),
        "targetUser": AdditionalAttributesCategoryConfig(enabled=False, emitLists=False),
    }

    def enabled_category_names(self) -> set[str]:
        """Return the set of categories whose ``enabled`` flag is on."""
        return {name for name, cfg in self.categories.items() if cfg.enabled}

    def emit_list_categories(self) -> set[str]:
        """Return the set of categories whose staging view should exist.

        Requires both ``enabled`` (parser populates the columns) and
        ``emitLists`` (the view is materialised). A category with
        columns populated but no view is a valid opt-in: local querying
        works, but the reporting model's list import isn't wired.
        """
        return {name for name, cfg in self.categories.items() if cfg.enabled and cfg.emitLists}


class ModelHistoryTargetModel(BaseModel):
    """Target Anaplan model for the Model History upload.

    Deliberately separate from :class:`TargetModelConfig` (the audit
    reporting model). Model history grows a model far faster than audit
    reporting, so it lives in its own Anaplan model that a UX page can
    display side-by-side with the audit model — and keeping the two apart
    makes each easier to iterate on independently.

    Only workspace + model are needed here: the process to run comes from
    :attr:`ModelHistoryConfig.anaplanProcess`, and the three CSV data
    sources have fixed names (``MODEL_REGISTRY.csv``,
    ``MODEL_HISTORY_LIST.csv``, ``MODEL_HISTORY_NORMALIZED.csv``).
    """

    model_config = ConfigDict(populate_by_name=True)

    workspaceId: str = ""
    modelId: str = ""


class ModelHistoryConfig(BaseModel):
    """Configuration for the Anaplan Model History feature."""

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False

    targetAnaplanModel: ModelHistoryTargetModel = ModelHistoryTargetModel()
    """The Anaplan model that receives the model-history CSVs — separate
    from the audit reporting model (``targetAnaplanModel`` at the top
    level). Required (workspaceId + modelId) whenever ``enabled`` is true;
    the settings validator raises :class:`ConfigError` otherwise."""

    exportActionName: str = "MODEL_HISTORY_EXPORT"
    exportTimeoutSeconds: int = 600
    retentionYears: int = 2
    anaplanProcess: str = "Load Model History"

    # --- Concurrency ---
    maxConcurrentExports: int = 5
    """Maximum parallel model history exports.

    Each worker fires the export task, polls for completion, and downloads
    the result independently.  Database writes are always serialised on the
    main thread after all exports finish.  Raise this value for tenants with
    many models; lower it if you hit API rate limits.
    """

    # --- Backup ---
    backupBeforePurge: bool = True
    """Create a timestamped backup of the database before each purge.

    Backups are written alongside the database file with the suffix
    ``_backup_YYYYMMDD_HHMMSS.db``.  Old backups beyond *maxBackupsToKeep*
    are removed automatically.
    """

    maxBackupsToKeep: int = 7
    """Number of most-recent backups to retain when *backupBeforePurge* is true."""


class Settings(BaseSettings):
    """Top-level application settings.

    Field names match the v1 ``settings.json`` keys exactly so that existing
    customer configuration files work without modification.
    """

    model_config = SettingsConfigDict(
        env_prefix="ANAPLAN_AUDIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Feature flags ---
    auditEnabled: bool = True
    """Run the full audit extract-transform-load pipeline (Steps 1-6).

    Set to ``false`` to skip the audit entirely and run only Model History.
    At least one of ``auditEnabled`` or ``modelHistory.enabled`` must be
    ``true``; the validator below will raise a :class:`ConfigError` otherwise.
    """

    # --- Core ---
    authenticationMode: Literal["basic", "cert_auth", "OAuth"] = "OAuth"
    anaplanTenantName: str = ""
    database: str = "anaplan_audit.duckdb"
    """Path to the local DuckDB database file.

    v4 uses DuckDB (v1-v3 used SQLite). The engines' file formats are
    incompatible — pointing v4 at an old ``.db`` SQLite file fails loudly
    at open, so existing configs that pin the old name must either adopt
    the new default or pick a fresh filename.
    """
    lastRun: int = 0
    auditBatchSize: int = 1000
    auditRetentionYears: int = 0
    """Purge audit events older than this many years from the local database.

    ``0`` (the default) keeps every event forever — the historical v1/v3
    behaviour.  When set, a timestamped backup is taken before each purge,
    using the same rolling-backup window as Model History.
    """
    workspaceModelFilterApproach: Literal["select", "skip"] = "select"
    workspaceModelCombos: list[WorkspaceModelCombo] = []

    # --- URIs ---
    uris: AnaplanUris = AnaplanUris()

    # --- Target model ---
    targetAnaplanModel: TargetModelConfig = TargetModelConfig()

    # --- Model History ---
    modelHistory: ModelHistoryConfig = ModelHistoryConfig()

    # --- additionalAttributes extractor (v3.3.0) ---
    additionalAttributes: AdditionalAttributesConfig = AdditionalAttributesConfig()

    # --- Cert auth ---
    certPublicPath: str = ""
    certPrivatePath: str = ""
    certPassphrase: str = ""
    """Passphrase for the private key.

    Preferred over the legacy ``certPrivatePath = "path:passphrase"`` inline
    form, which is ambiguous with Windows drive letters.  When set, the
    private-key path is used verbatim (no ``:`` parsing).
    """

    # --- OAuth ---
    oauthClientId: str = ""
    """The OAuth client ID used for device registration and token refresh.

    Required when ``authenticationMode`` is ``OAuth``.  The ``register``
    command persists this automatically after a successful registration, so
    most users never set it by hand.
    """

    rotatableToken: bool = True

    # --- Basic auth (env-only, never in settings.json) ---
    basic_username: str = ""
    basic_password: str = ""

    # Path of the settings.json this instance was loaded from, so writes
    # (e.g. the lastRun watermark) go back to the same file.  None when no
    # file existed at load time.
    _source_path: Path | None = PrivateAttr(default=None)

    @property
    def source_path(self) -> Path | None:
        """The settings.json path this instance was loaded from, if any."""
        return self._source_path

    def resolved_cert_paths(self) -> tuple[Path, Path, str | None]:
        """Return ``(public_path, private_path, passphrase)`` for cert auth.

        The public certificate never carries a passphrase and is used
        verbatim.  The private-key passphrase comes from ``certPassphrase``
        when set, otherwise from an inline ``:passphrase`` suffix parsed in
        a Windows-drive-letter-safe way.  Both the startup validator and the
        auth dispatch go through here so the parsing can never diverge.
        """
        pub = Path(self.certPublicPath)
        if self.certPassphrase:
            return pub, Path(self.certPrivatePath), self.certPassphrase
        priv_str, passphrase = split_cert_path_and_passphrase(self.certPrivatePath)
        return pub, Path(priv_str), passphrase

    @field_validator("lastRun")
    @classmethod
    def _warn_stale_last_run(cls, v: int) -> int:
        """Warn if lastRun is more than 30 days old."""
        import time

        if v > 0:
            age_days = (time.time() - v) / 86400
            if age_days > 30:
                warnings.warn(
                    f"lastRun is {age_days:.0f} days old; "
                    "Anaplan only retains 30 days of audit data.",
                    UserWarning,
                    stacklevel=2,
                )
        return v

    @model_validator(mode="after")
    def _validate_feature_flags(self) -> Settings:
        """Ensure at least one feature is enabled."""
        if not self.auditEnabled and not self.modelHistory.enabled:
            raise ConfigError(
                "Both auditEnabled and modelHistory.enabled are false — "
                "nothing to do. Enable at least one feature.",
            )
        return self

    @model_validator(mode="after")
    def _validate_model_history_target(self) -> Settings:
        """Require a dedicated model-history target model when enabled.

        Model history uploads to its own Anaplan model, distinct from the
        audit reporting model. There is intentionally no fallback to
        ``targetAnaplanModel`` — the separation must be explicit so history
        (which grows fast) never lands in the audit model by accident.
        """
        mh = self.modelHistory
        if mh.enabled and (
            not mh.targetAnaplanModel.workspaceId or not mh.targetAnaplanModel.modelId
        ):
            raise ConfigError(
                "modelHistory.enabled is true but modelHistory.targetAnaplanModel "
                "is not fully configured — set both workspaceId and modelId. "
                "Model history uploads to its own Anaplan model, separate from "
                "the audit reporting model (targetAnaplanModel).",
                context={
                    "workspaceId": mh.targetAnaplanModel.workspaceId,
                    "modelId": mh.targetAnaplanModel.modelId,
                },
            )
        return self

    @model_validator(mode="after")
    def _validate_auth_requirements(self) -> Settings:
        """Validate auth-mode-specific requirements at startup."""
        if self.authenticationMode == "cert_auth":
            if not self.certPublicPath or not self.certPrivatePath:
                raise ConfigError(
                    "cert_auth mode requires both certPublicPath and "
                    "certPrivatePath in settings.json.",
                )
            pub, priv, _ = self.resolved_cert_paths()
            if not pub.exists():
                raise ConfigError(
                    f"Certificate public key not found: {pub}",
                    context={"path": str(pub)},
                )
            if not priv.exists():
                raise ConfigError(
                    f"Certificate private key not found: {priv}",
                    context={"path": str(priv)},
                )
        return self


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings with JSON file as a base layer.

    Args:
        config_path: Path to ``settings.json``.  Defaults to ``./settings.json``.

    Returns:
        A validated :class:`Settings` instance.
    """
    path = config_path or Path("settings.json")
    init_kwargs: dict[str, Any] = {}
    file_exists = path.exists()
    if file_exists:
        with open(path) as f:
            init_kwargs = json.load(f)
    settings = Settings(**init_kwargs)
    if file_exists:
        settings._source_path = path
    return settings
