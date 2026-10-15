"""Errors raised by docprovider."""

from __future__ import annotations


class DocProviderError(Exception):
    """Base class for all docprovider errors."""


class LoginError(DocProviderError):
    """Account login was rejected (bad credentials or account locked)."""


class CookiesExpiredError(DocProviderError):
    """Yandex cookies are missing, expired or were rejected."""


class CaptchaRequiredError(DocProviderError):
    """The service demanded a captcha we cannot solve automatically."""


class ApiError(DocProviderError):
    """A carrier API answered with an unexpected status or body."""

    def __init__(self, service: str, status: int, detail: str = "") -> None:
        self.service = service
        self.status = status
        self.detail = detail
        super().__init__(f"{service}: HTTP {status}" + (f": {detail}" if detail else ""))


class ValidationError(DocProviderError):
    """A produced or supplied carrier URL does not look valid."""


class AllAccountsExhaustedError(DocProviderError):
    """Every account in the pool hit its daily document limit."""


class OpenFluxBinaryError(DocProviderError):
    """The openflux binary is missing or returned an error."""
