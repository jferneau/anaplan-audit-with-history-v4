"""Tests for configuration loading and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from anaplan_audit.config import Settings, load_settings
from anaplan_audit.exceptions import ConfigError


class TestSettingsLoad:
    """Test settings loading from various sources."""

    def test_load_from_json(self, tmp_path: Path) -> None:
        """Settings load correctly from a JSON file."""
        config = {
            "authenticationMode": "basic",
            "anaplanTenantName": "TestTenant",
            "database": "test.db",
            "lastRun": 0,
            "auditBatchSize": 500,
            "workspaceModelFilterApproach": "select",
            "workspaceModelCombos": [{"workspaceId": "ws1", "modelId": "m1"}],
            "writeSampleFilesOverride": False,
            "uris": {
                "authServiceUri": "https://example.com/auth",
                "authTokenVerify": "https://example.com/verify",
                "oauthServiceUri": "https://example.com/oauth",
                "integrationUri": "https://example.com/api",
                "auditUri": "https://example.com/audit",
                "scimUri": "https://example.com/scim",
                "cloudWorksUri": "https://example.com/cloudworks",
            },
            "targetAnaplanModel": {
                "workspaceId": "ws1",
                "modelId": "m1",
                "objects": {
                    "auditFileId": "f1",
                    "auditImportId": "i1",
                    "lastRunFileId": "f2",
                    "lastRunImportId": "i2",
                },
            },
        }
        config_path = tmp_path / "settings.json"
        config_path.write_text(json.dumps(config))

        settings = load_settings(config_path)
        assert settings.authenticationMode == "basic"
        assert settings.anaplanTenantName == "TestTenant"
        assert settings.auditBatchSize == 500
        assert len(settings.workspaceModelCombos) == 1

    def test_load_defaults_when_no_file(self, tmp_path: Path) -> None:
        """Settings fall back to defaults when no config file exists."""
        settings = load_settings(tmp_path / "nonexistent.json")
        assert settings.authenticationMode == "OAuth"
        assert settings.database == "anaplan_audit.duckdb"

    def test_invalid_auth_mode(self) -> None:
        """Invalid authenticationMode raises validation error."""
        with pytest.raises(ValidationError):
            Settings(authenticationMode="invalid")  # type: ignore[arg-type]

    def test_cert_auth_missing_cert_warns(self, tmp_path: Path) -> None:
        """cert_auth mode with missing cert file raises ConfigError."""
        with pytest.raises(ConfigError):
            Settings(
                authenticationMode="cert_auth",
                certPublicPath=str(tmp_path / "nonexistent.pem"),
                certPrivatePath=str(tmp_path / "nonexistent.key"),
            )

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Environment variables override JSON settings."""
        config = {"authenticationMode": "basic", "anaplanTenantName": "FromJson"}
        config_path = tmp_path / "settings.json"
        config_path.write_text(json.dumps(config))
        monkeypatch.setenv("ANAPLAN_AUDIT_BASIC_USERNAME", "testuser")
        monkeypatch.setenv("ANAPLAN_AUDIT_BASIC_PASSWORD", "testpass")

        settings = load_settings(config_path)
        assert settings.basic_username == "testuser"
        assert settings.basic_password == "testpass"

    def test_audit_enabled_defaults_true(self) -> None:
        """auditEnabled defaults to True for backwards compatibility."""
        settings = Settings()
        assert settings.auditEnabled is True

    def test_model_history_enabled_defaults_false(self) -> None:
        """modelHistory.enabled defaults to False (opt-in feature)."""
        settings = Settings()
        assert settings.modelHistory.enabled is False

    def test_both_features_disabled_raises(self) -> None:
        """Setting both auditEnabled=False and modelHistory.enabled=False raises ConfigError."""
        with pytest.raises(ConfigError):
            Settings(
                auditEnabled=False,
                modelHistory={"enabled": False},  # type: ignore[arg-type]
            )

    def test_audit_disabled_model_history_enabled_is_valid(self) -> None:
        """auditEnabled=False with modelHistory.enabled=True is a valid configuration."""
        settings = Settings(
            auditEnabled=False,
            modelHistory={  # type: ignore[arg-type]
                "enabled": True,
                "targetAnaplanModel": {"workspaceId": "mh-ws", "modelId": "mh-model"},
            },
        )
        assert settings.auditEnabled is False
        assert settings.modelHistory.enabled is True
        assert settings.modelHistory.targetAnaplanModel.workspaceId == "mh-ws"

    def test_model_history_enabled_without_target_raises(self) -> None:
        """modelHistory.enabled=True with no targetAnaplanModel is a ConfigError.

        Model history uploads to its own model — there is no fallback to the
        audit reporting model, so the separate target must be explicit.
        """
        with pytest.raises(ConfigError, match="targetAnaplanModel"):
            Settings(
                auditEnabled=False,
                modelHistory={"enabled": True},  # type: ignore[arg-type]
            )

    def test_model_history_enabled_with_partial_target_raises(self) -> None:
        """A target missing modelId (or workspaceId) is rejected."""
        with pytest.raises(ConfigError, match="workspaceId and modelId"):
            Settings(
                auditEnabled=False,
                modelHistory={  # type: ignore[arg-type]
                    "enabled": True,
                    "targetAnaplanModel": {"workspaceId": "mh-ws"},
                },
            )

    def test_model_history_disabled_ignores_missing_target(self) -> None:
        """When disabled, the target is not required (default full-audit run)."""
        settings = Settings()  # audit on, model history off by default
        assert settings.modelHistory.enabled is False
        assert settings.modelHistory.targetAnaplanModel.workspaceId == ""

    def test_model_history_max_concurrent_exports_default(self) -> None:
        """maxConcurrentExports defaults to 5."""
        settings = Settings()
        assert settings.modelHistory.maxConcurrentExports == 5

    def test_model_history_backup_before_purge_default(self) -> None:
        """backupBeforePurge defaults to True."""
        settings = Settings()
        assert settings.modelHistory.backupBeforePurge is True

    def test_model_history_max_backups_to_keep_default(self) -> None:
        """maxBackupsToKeep defaults to 7."""
        settings = Settings()
        assert settings.modelHistory.maxBackupsToKeep == 7

    def test_model_history_new_keys_load_from_json(self, tmp_path: Path) -> None:
        """maxConcurrentExports, backupBeforePurge, maxBackupsToKeep load from settings.json."""
        config = {
            "modelHistory": {
                "enabled": True,
                "targetAnaplanModel": {"workspaceId": "mh-ws", "modelId": "mh-model"},
                "maxConcurrentExports": 3,
                "backupBeforePurge": False,
                "maxBackupsToKeep": 14,
            }
        }
        config_path = tmp_path / "settings.json"
        config_path.write_text(__import__("json").dumps(config))

        settings = load_settings(config_path)
        assert settings.modelHistory.maxConcurrentExports == 3
        assert settings.modelHistory.backupBeforePurge is False
        assert settings.modelHistory.maxBackupsToKeep == 14
