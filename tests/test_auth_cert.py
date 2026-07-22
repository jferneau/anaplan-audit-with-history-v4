"""Tests for certificate-based authentication (v2 single-POST format)."""

from __future__ import annotations

import datetime
from pathlib import Path

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from anaplan_audit.auth.cert import authenticate_cert
from anaplan_audit.config import AnaplanUris
from anaplan_audit.exceptions import CertAuthError

# ---------------------------------------------------------------------------
# Fixture — generates a real self-signed X.509 cert + RSA private key
# ---------------------------------------------------------------------------


@pytest.fixture()
def cert_files(tmp_path: Path) -> tuple[Path, Path]:
    """Generate a self-signed X.509 certificate and RSA private key."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365))
        .sign(private_key, hashes.SHA256())
    )

    pub_path = tmp_path / "test_cert.pem"
    priv_path = tmp_path / "test_key.pem"
    pub_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    priv_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return pub_path, priv_path


_TOKEN_RESPONSE = {
    "tokenInfo": {"tokenValue": "cert-token-456"},
    "status": "SUCCESS",
}


class TestCertAuthV2:
    """v2 single-POST cert auth flow."""

    def test_success_returns_token_on_201(self, cert_files: tuple[Path, Path]) -> None:
        """Anaplan returns HTTP 201 for successful cert auth — must accept it."""
        pub_path, priv_path = cert_files
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")

        with respx.mock:
            route = respx.post("https://mock.anaplan.com/auth").mock(
                return_value=httpx.Response(201, json=_TOKEN_RESPONSE)
            )
            token = authenticate_cert(pub_path, priv_path, None, uris)

        assert token.access_token == "cert-token-456"
        assert route.call_count == 1  # v2 is a single POST, not two

    def test_success_returns_token_on_200(self, cert_files: tuple[Path, Path]) -> None:
        """HTTP 200 is also accepted for environments that return it."""
        pub_path, priv_path = cert_files
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")

        with respx.mock:
            respx.post("https://mock.anaplan.com/auth").mock(
                return_value=httpx.Response(200, json=_TOKEN_RESPONSE)
            )
            token = authenticate_cert(pub_path, priv_path, None, uris)

        assert token.access_token == "cert-token-456"

    def test_single_post_only(self, cert_files: tuple[Path, Path]) -> None:
        """v2 auth must make exactly one HTTP request."""
        pub_path, priv_path = cert_files
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")

        with respx.mock:
            route = respx.post("https://mock.anaplan.com/auth").mock(
                return_value=httpx.Response(200, json=_TOKEN_RESPONSE)
            )
            authenticate_cert(pub_path, priv_path, None, uris)

        assert route.call_count == 1

    def test_request_has_cacertificate_header(self, cert_files: tuple[Path, Path]) -> None:
        """Authorization header must start with 'CACertificate '."""
        pub_path, priv_path = cert_files
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")
        captured: list[httpx.Request] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=_TOKEN_RESPONSE)

        with respx.mock:
            respx.post("https://mock.anaplan.com/auth").mock(side_effect=_capture)
            authenticate_cert(pub_path, priv_path, None, uris)

        auth = captured[0].headers["authorization"]
        assert auth.startswith("CACertificate ")

    def test_request_body_has_v2_format(self, cert_files: tuple[Path, Path]) -> None:
        """POST body must include encodedDataFormat='v2', encodedData, encodedSignedData."""
        import json

        pub_path, priv_path = cert_files
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")
        captured: list[httpx.Request] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=_TOKEN_RESPONSE)

        with respx.mock:
            respx.post("https://mock.anaplan.com/auth").mock(side_effect=_capture)
            authenticate_cert(pub_path, priv_path, None, uris)

        body = json.loads(captured[0].content)
        assert body["encodedDataFormat"] == "v2"
        assert "encodedData" in body
        assert "encodedSignedData" in body

    def test_token_fallback_to_token_key(self, cert_files: tuple[Path, Path]) -> None:
        """Falls back to tokenInfo.token if tokenInfo.tokenValue is absent."""
        pub_path, priv_path = cert_files
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")

        with respx.mock:
            respx.post("https://mock.anaplan.com/auth").mock(
                return_value=httpx.Response(
                    200, json={"tokenInfo": {"token": "fallback-token"}, "status": "SUCCESS"}
                )
            )
            token = authenticate_cert(pub_path, priv_path, None, uris)

        assert token.access_token == "fallback-token"

    def test_http_non_200_raises_cert_auth_error(self, cert_files: tuple[Path, Path]) -> None:
        """Any non-200 HTTP status raises CertAuthError."""
        pub_path, priv_path = cert_files
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")

        with respx.mock:
            respx.post("https://mock.anaplan.com/auth").mock(
                return_value=httpx.Response(401, json={"error": "Unauthorized"})
            )
            with pytest.raises(CertAuthError) as exc_info:
                authenticate_cert(pub_path, priv_path, None, uris)

        assert "401" in str(exc_info.value)

    def test_failure_status_in_body_raises_cert_auth_error(
        self, cert_files: tuple[Path, Path]
    ) -> None:
        """A 200 response with FAILURE status raises CertAuthError."""
        pub_path, priv_path = cert_files
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")

        with respx.mock:
            respx.post("https://mock.anaplan.com/auth").mock(
                return_value=httpx.Response(
                    200,
                    json={"status": "FAILURE", "statusMessage": "Invalid certificate"},
                )
            )
            with pytest.raises(CertAuthError, match="FAILURE"):
                authenticate_cert(pub_path, priv_path, None, uris)

    def test_missing_token_in_response_raises_cert_auth_error(
        self, cert_files: tuple[Path, Path]
    ) -> None:
        """A 200 response with no tokenValue raises CertAuthError."""
        pub_path, priv_path = cert_files
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")

        with respx.mock:
            respx.post("https://mock.anaplan.com/auth").mock(
                return_value=httpx.Response(200, json={"tokenInfo": {}, "status": "SUCCESS"})
            )
            with pytest.raises(CertAuthError):
                authenticate_cert(pub_path, priv_path, None, uris)

    def test_missing_cert_files_raise_cert_auth_error(self, tmp_path: Path) -> None:
        """Missing certificate files raise CertAuthError."""
        uris = AnaplanUris()
        with pytest.raises(CertAuthError):
            authenticate_cert(
                tmp_path / "nonexistent.pem",
                tmp_path / "nonexistent.key",
                None,
                uris,
            )
