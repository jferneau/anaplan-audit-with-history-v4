"""Smoke tests for the CLI using typer.testing.CliRunner."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anaplan_audit.auth.models import AuthToken
from anaplan_audit.cli import app

runner = CliRunner()


class TestCLI:
    """CLI smoke tests."""

    def test_version(self) -> None:
        """The version command prints version info."""
        from anaplan_audit import __version__

        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_validate_config_with_valid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """validate-config succeeds and reports the auth test result."""
        config = {
            "authenticationMode": "basic",
            "anaplanTenantName": "Test",
            "database": "test.db",
        }
        config_path = tmp_path / "settings.json"
        config_path.write_text(json.dumps(config))

        token = AuthToken(
            access_token="test-token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        monkeypatch.setattr("anaplan_audit.cli._test_authentication", lambda _s: token)

        result = runner.invoke(app, ["validate-config", "--config", str(config_path)])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()
        assert "authentication succeeded" in result.output.lower()

    def test_validate_config_skip_auth(self, tmp_path: Path) -> None:
        """--skip-auth validates settings without touching credentials."""
        config = {
            "authenticationMode": "basic",
            "anaplanTenantName": "Test",
            "database": "test.db",
        }
        config_path = tmp_path / "settings.json"
        config_path.write_text(json.dumps(config))

        result = runner.invoke(
            app, ["validate-config", "--config", str(config_path), "--skip-auth"]
        )
        assert result.exit_code == 0
        assert "skipped" in result.output.lower()

    def test_validate_config_auth_failure_sets_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing auth test surfaces the typed exit code (3)."""
        from anaplan_audit.exceptions import AuthError

        config_path = tmp_path / "settings.json"
        config_path.write_text(json.dumps({"authenticationMode": "basic"}))

        def _boom(_s: object) -> AuthToken:
            raise AuthError("bad credentials")

        monkeypatch.setattr("anaplan_audit.cli._test_authentication", _boom)

        result = runner.invoke(app, ["validate-config", "--config", str(config_path)])
        assert result.exit_code == 3
        assert "authentication failed" in result.output.lower()

    def test_validate_config_no_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """validate-config with nonexistent file uses defaults."""
        token = AuthToken(
            access_token="test-token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        monkeypatch.setattr("anaplan_audit.cli._test_authentication", lambda _s: token)
        result = runner.invoke(app, ["validate-config", "--config", "/tmp/nonexistent_config.json"])
        assert result.exit_code == 0

    def test_help(self) -> None:
        """--help shows all subcommands."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "run" in result.output
        assert "register" in result.output
        assert "validate-config" in result.output
        assert "version" in result.output
