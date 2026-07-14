"""Shared authenticated and TLS-verified Qdrant transport configuration."""

from __future__ import annotations

import ipaddress
import os
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


_API_KEY_ENV = "QDRANT_API_KEY"
_CA_CERT_ENV = "QDRANT_CA_CERT"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn redirects into HTTP errors so credentials never change origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _is_literal_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _load_ca_path(raw_path: str | None) -> Path | None:
    if not raw_path or not raw_path.strip():
        return None
    try:
        path = Path(raw_path.strip()).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError
        ssl.create_default_context(cafile=str(path))
    except (OSError, RuntimeError, ValueError, ssl.SSLError):
        raise ValueError("QDRANT_CA_CERT must reference a readable CA certificate file") from None
    return path


@dataclass(frozen=True)
class QdrantTransportConfig:
    """Immutable Qdrant credential and trust configuration."""

    api_key: str | None = field(default=None, repr=False)
    ca_cert: Path | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> QdrantTransportConfig:
        source = os.environ if environ is None else environ
        api_key = source.get(_API_KEY_ENV) or None
        return cls(api_key=api_key, ca_cert=_load_ca_path(source.get(_CA_CERT_ENV)))

    def ssl_context(self) -> ssl.SSLContext:
        """Return a verified context using the custom CA or system trust."""
        try:
            cafile = str(self.ca_cert) if self.ca_cert is not None else None
            return ssl.create_default_context(cafile=cafile)
        except (OSError, ValueError, ssl.SSLError):
            raise ValueError("QDRANT_CA_CERT could not be loaded") from None

    def headers(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Return fresh request headers with the Qdrant key scoped to them."""
        result = {str(name): str(value) for name, value in (base or {}).items() if str(name).lower() != "api-key"}
        if self.api_key:
            result["api-key"] = self.api_key
        return result

    def validate_url(self, url: str) -> str:
        """Reject malformed or remotely reachable plaintext Qdrant URLs."""
        try:
            parsed = urllib.parse.urlsplit(url)
            hostname = parsed.hostname
            _ = parsed.port
        except (TypeError, ValueError):
            raise ValueError("Qdrant URL must be a valid HTTP(S) URL") from None
        if parsed.scheme not in {"http", "https"} or not hostname:
            raise ValueError("Qdrant URL must be a valid HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Qdrant URL must not contain credentials")
        if parsed.scheme == "http" and (self.ca_cert is not None or not _is_literal_loopback(hostname)):
            raise ValueError("Qdrant URL must use HTTPS except for explicit loopback development endpoints")
        return url

    def httpx_kwargs(self) -> dict[str, object]:
        """Build kwargs for an HTTPX client dedicated to Qdrant."""
        kwargs: dict[str, object] = {}
        if self.api_key:
            kwargs["headers"] = self.headers()
        if self.ca_cert is not None:
            kwargs["verify"] = self.ssl_context()
        return kwargs

    def qdrant_client_kwargs(self) -> dict[str, object]:
        """Build optional kwargs for ``qdrant_client.QdrantClient``."""
        kwargs: dict[str, object] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.ca_cert is not None:
            kwargs["verify"] = self.ssl_context()
        return kwargs

    def urlopen_kwargs(self) -> dict[str, ssl.SSLContext]:
        """Build urllib kwargs without disabling system verification."""
        if self.ca_cert is None:
            return {}
        return {"context": self.ssl_context()}

    def open(self, request: urllib.request.Request, *, timeout: float) -> Any:
        """Open one Qdrant request with verified TLS and redirects disabled."""
        parsed = urllib.parse.urlsplit(self.validate_url(request.full_url))
        handlers: list[urllib.request.BaseHandler] = [_NoRedirectHandler()]
        if parsed.scheme == "https":
            handlers.append(urllib.request.HTTPSHandler(context=self.ssl_context()))
        return urllib.request.build_opener(*handlers).open(request, timeout=timeout)

    def request(
        self,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        method: str | None = None,
    ) -> urllib.request.Request:
        """Build a Qdrant-only urllib request with scoped credentials."""
        self.validate_url(url)
        return urllib.request.Request(
            url,
            data=data,
            headers=self.headers(headers),
            method=method,
        )
