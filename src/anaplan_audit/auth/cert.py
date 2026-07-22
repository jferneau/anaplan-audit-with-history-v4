"""Certificate-based authentication — Anaplan v2 payload format.

v2 Format (single POST, no two-step nonce challenge):
  - 100-byte message: 8-byte UTC epoch timestamp (big-endian) + 92 random bytes
  - encodedData:       base64(message_bytes)
  - encodedSignedData: base64(RSA-SHA512/PKCS1v15 signature of message_bytes)
  - POST body:         {"encodedDataFormat": "v2", "encodedData": "...", "encodedSignedData": "..."}
  - Authorization:     CACertificate <DER-encoded cert, base64>
"""

from __future__ import annotations

import os
import struct
import time
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import structlog
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.x509 import load_pem_x509_certificate

from anaplan_audit.auth.models import AuthToken
from anaplan_audit.config import AnaplanUris
from anaplan_audit.exceptions import CertAuthError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

_PAYLOAD_VERSION = "v2"
_PAYLOAD_BYTES = 100
_TIMESTAMP_BYTES = 8
_RANDOM_BYTES = _PAYLOAD_BYTES - _TIMESTAMP_BYTES  # 92


def _load_public_cert_b64(public_path: Path) -> str:
    """DER-encode a PEM X.509 certificate and return it as a base64 string.

    Anaplan expects the raw DER bytes base64-encoded (no PEM headers).
    """
    cert = load_pem_x509_certificate(public_path.read_bytes())
    return b64encode(cert.public_bytes(serialization.Encoding.DER)).decode("ascii")


def _load_private_key(private_path: Path, passphrase: str | None) -> RSAPrivateKey:
    """Load a PEM private key, optionally passphrase-protected.

    The orchestrator already splits the ``path:passphrase`` format before
    calling :func:`authenticate_cert`, so ``private_path`` is always a plain
    file path here.
    """
    pwd = passphrase.encode() if passphrase else None
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=pwd)
    if not isinstance(key, RSAPrivateKey):
        raise CertAuthError(
            "Private key is not an RSA key — Anaplan cert auth requires RSA",
            context={"key_type": type(key).__name__},
        )
    return key


def _build_v2_payload(private_key: RSAPrivateKey) -> dict[str, str]:
    """Build the v2 authentication payload.

    Generates a fresh 100-byte message on every call — the timestamp component
    ensures each request is unique and replay-resistant.
    """
    timestamp_bytes = struct.pack(">Q", int(time.time()))  # 8 bytes, big-endian
    message_bytes = timestamp_bytes + os.urandom(_RANDOM_BYTES)  # 100 bytes total

    encoded_data = b64encode(message_bytes).decode("ascii")
    signature = private_key.sign(message_bytes, padding.PKCS1v15(), hashes.SHA512())
    encoded_signed_data = b64encode(signature).decode("ascii")

    return {
        "encodedDataFormat": _PAYLOAD_VERSION,
        "encodedData": encoded_data,
        "encodedSignedData": encoded_signed_data,
    }


def authenticate_cert(
    public_path: Path,
    private_path: Path,
    passphrase: str | None,
    uris: AnaplanUris,
) -> AuthToken:
    """Authenticate via Anaplan certificate auth (v2 single-POST format).

    Args:
        public_path: Path to the PEM X.509 public certificate.
        private_path: Path to the PEM RSA private key.
        passphrase: Optional passphrase for the private key.  The
            ``path:passphrase`` splitting is handled by the caller
            (orchestrator) before this function is invoked.
        uris: API base URIs — uses :attr:`~AnaplanUris.authServiceUri`.

    Returns:
        A valid :class:`~anaplan_audit.auth.models.AuthToken`.

    Raises:
        CertAuthError: On load failure, HTTP error, or missing token.
    """
    try:
        cert_b64 = _load_public_cert_b64(public_path)
        private_key = _load_private_key(private_path, passphrase)
    except CertAuthError:
        raise
    except Exception as exc:
        raise CertAuthError(
            f"Failed to load certificate files: {exc}",
            context={"public_path": str(public_path), "private_path": str(private_path)},
        ) from exc

    payload = _build_v2_payload(private_key)

    logger.debug(
        "cert_auth_request",
        payload_format=_PAYLOAD_VERSION,
        encoded_data_preview=payload["encodedData"][:24] + "...",
    )

    try:
        with httpx.Client(http2=True, timeout=60.0) as client:
            response = client.post(
                uris.authServiceUri,
                headers={
                    "Authorization": f"CACertificate {cert_b64}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise CertAuthError(
            f"Cert auth request error: {exc}",
            context={"error": str(exc)},
        ) from exc

    logger.debug(
        "cert_auth_response",
        status_code=response.status_code,
        response_body=response.text[:500],
    )

    if response.status_code not in (200, 201):
        raise CertAuthError(
            f"Cert auth failed: HTTP {response.status_code}",
            context={"status_code": response.status_code, "body": response.text[:500]},
        )

    resp_json = response.json()

    status = resp_json.get("status", "").upper()
    if "FAILURE" in status:
        message = resp_json.get("statusMessage", resp_json.get("message", "no message"))
        raise CertAuthError(
            f"Cert auth returned failure status '{status}': {message}",
            context={"status": status, "statusMessage": message},
        )

    token_info = resp_json.get("tokenInfo", {})
    token_value = token_info.get("tokenValue") or token_info.get("token")

    if not token_value:
        raise CertAuthError(
            "Could not find token in auth response",
            context={"tokenInfo_keys": list(token_info.keys())},
        )

    logger.info("cert_auth_success", payload_format=_PAYLOAD_VERSION)

    return AuthToken(
        access_token=token_value,
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=AuthToken.TOKEN_LIFETIME_MINUTES),
    )
