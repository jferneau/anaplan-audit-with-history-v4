"""Regression tests for the v3.1.1 bug-fix batch.

Covers:
- BUG 1: OAuth uses ``oauthClientId``, not the target model ID.
- BUG 2: ``lastRun`` persists to the file settings were loaded from.
- BUG 4: Default URIs point at the current Anaplan hosts.
- BUG 7: ``cert_auth`` requires non-empty cert paths at validation time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anaplan_audit.config import (
    AnaplanUris,
    Settings,
    load_settings,
    split_cert_path_and_passphrase,
)
from anaplan_audit.exceptions import ConfigError


class TestOAuthClientId:
    """BUG 1 — oauthClientId setting drives the OAuth flow."""

    def test_default_is_empty(self) -> None:
        assert Settings().oauthClientId == ""

    def test_loaded_from_json(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"oauthClientId": "my-client-id"}))
        settings = load_settings(path)
        assert settings.oauthClientId == "my-client-id"

    def test_authenticate_oauth_without_client_id_raises(self) -> None:
        from anaplan_audit.orchestrator import _authenticate

        settings = Settings(authenticationMode="OAuth", oauthClientId="")
        with pytest.raises(ConfigError, match="oauthClientId"):
            _authenticate(settings)

    def test_authenticate_oauth_uses_client_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The client ID passed to refresh_access_token is oauthClientId."""
        from anaplan_audit import orchestrator

        captured: dict[str, str] = {}

        def _fake_refresh(client_id: str, *args: object, **kwargs: object) -> None:
            captured["client_id"] = client_id
            raise RuntimeError("stop here")

        monkeypatch.setattr(orchestrator, "refresh_access_token", _fake_refresh)
        settings = Settings(authenticationMode="OAuth", oauthClientId="the-real-id")
        with pytest.raises(RuntimeError, match="stop here"):
            orchestrator._authenticate(settings)
        assert captured["client_id"] == "the-real-id"


class TestLastRunSourcePath:
    """BUG 2 — lastRun writes back to the loaded config path."""

    def test_source_path_set_when_file_exists(self, tmp_path: Path) -> None:
        path = tmp_path / "custom-settings.json"
        path.write_text(json.dumps({"anaplanTenantName": "T"}))
        settings = load_settings(path)
        assert settings.source_path == path

    def test_source_path_none_when_no_file(self, tmp_path: Path) -> None:
        settings = load_settings(tmp_path / "missing.json")
        assert settings.source_path is None

    def test_update_last_run_writes_to_source_path(self, tmp_path: Path) -> None:
        from anaplan_audit.upload import _update_last_run

        path = tmp_path / "prod-settings.json"
        path.write_text(json.dumps({"anaplanTenantName": "T", "lastRun": 111}))
        settings = load_settings(path)

        _update_last_run(settings, 999_999)

        raw = json.loads(path.read_text())
        assert raw["lastRun"] == 999_999
        # Other keys are preserved.
        assert raw["anaplanTenantName"] == "T"

    def test_source_path_survives_model_copy(self, tmp_path: Path) -> None:
        """--since uses model_copy; the source path must carry over."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"lastRun": 5}))
        settings = load_settings(path)
        copied = settings.model_copy(update={"lastRun": 42})
        assert copied.source_path == path


class TestDefaultUris:
    """BUG 4 — defaults match the documented current Anaplan hosts."""

    def test_auth_service_uri(self) -> None:
        assert AnaplanUris().authServiceUri == "https://auth.anaplan.com/token/authenticate"

    def test_auth_token_verify(self) -> None:
        assert AnaplanUris().authTokenVerify == "https://auth.anaplan.com/token/validate"

    def test_scim_uri(self) -> None:
        assert AnaplanUris().scimUri == "https://api.anaplan.com/scim/1/0/v2"

    def test_explicit_uris_still_override(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"uris": {"authServiceUri": "https://example.com/auth"}}))
        settings = load_settings(path)
        assert settings.uris.authServiceUri == "https://example.com/auth"


class TestCertAuthValidation:
    """BUG 7 — cert_auth demands non-empty, existing cert paths."""

    def test_empty_paths_rejected(self) -> None:
        with pytest.raises(ConfigError, match="certPublicPath"):
            Settings(authenticationMode="cert_auth")

    def test_missing_public_file_rejected(self, tmp_path: Path) -> None:
        priv = tmp_path / "key.pem"
        priv.write_text("key")
        with pytest.raises(ConfigError, match="not found"):
            Settings(
                authenticationMode="cert_auth",
                certPublicPath=str(tmp_path / "nope.pem"),
                certPrivatePath=str(priv),
            )

    def test_valid_paths_accepted(self, tmp_path: Path) -> None:
        pub = tmp_path / "cert.pem"
        priv = tmp_path / "key.pem"
        pub.write_text("cert")
        priv.write_text("key")
        settings = Settings(
            authenticationMode="cert_auth",
            certPublicPath=str(pub),
            certPrivatePath=str(priv),
        )
        assert settings.authenticationMode == "cert_auth"

    def test_passphrase_suffix_stripped_for_existence_check(self, tmp_path: Path) -> None:
        pub = tmp_path / "cert.pem"
        priv = tmp_path / "key.pem"
        pub.write_text("cert")
        priv.write_text("key")
        settings = Settings(
            authenticationMode="cert_auth",
            certPublicPath=str(pub),
            certPrivatePath=f"{priv}:s3cret",
        )
        assert settings.certPrivatePath.endswith(":s3cret")
        _, _, passphrase = settings.resolved_cert_paths()
        assert passphrase == "s3cret"

    def test_dedicated_passphrase_field(self, tmp_path: Path) -> None:
        """certPassphrase takes precedence; path is used verbatim."""
        pub = tmp_path / "cert.pem"
        priv = tmp_path / "key.pem"
        pub.write_text("cert")
        priv.write_text("key")
        settings = Settings(
            authenticationMode="cert_auth",
            certPublicPath=str(pub),
            certPrivatePath=str(priv),
            certPassphrase="s3cret",
        )
        _, resolved_priv, passphrase = settings.resolved_cert_paths()
        assert resolved_priv == priv
        assert passphrase == "s3cret"


class TestCertPathSplitting:
    """BUG (v3.2.1) — cert path/passphrase splitting must not eat Windows drive letters."""

    def test_windows_path_no_passphrase(self) -> None:
        path, passphrase = split_cert_path_and_passphrase(r"C:\certs\key.pem")
        assert path == r"C:\certs\key.pem"
        assert passphrase is None

    def test_windows_path_with_passphrase(self) -> None:
        path, passphrase = split_cert_path_and_passphrase(r"C:\certs\key.pem:s3cret")
        assert path == r"C:\certs\key.pem"
        assert passphrase == "s3cret"

    def test_windows_forward_slash_drive(self) -> None:
        path, passphrase = split_cert_path_and_passphrase("D:/certs/key.pem")
        assert path == "D:/certs/key.pem"
        assert passphrase is None

    def test_posix_path_no_passphrase(self) -> None:
        path, passphrase = split_cert_path_and_passphrase("/etc/anaplan/key.pem")
        assert path == "/etc/anaplan/key.pem"
        assert passphrase is None

    def test_posix_path_with_passphrase(self) -> None:
        path, passphrase = split_cert_path_and_passphrase("/etc/anaplan/key.pem:s3cret")
        assert path == "/etc/anaplan/key.pem"
        assert passphrase == "s3cret"
