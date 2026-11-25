"""Exceptions for the EMMASA API client."""

from __future__ import annotations


class EmmasaApiError(Exception):
    """Base exception for EMMASA API client errors."""


class EmmasaAuthError(EmmasaApiError):
    """Authentication or authorization error."""


class EmmasaMfaRequiredError(EmmasaAuthError):
    """Account requires SMS MFA (loginA2P) which is unsupported in headless mode."""


class EmmasaPasswordChangeRequiredError(EmmasaAuthError):
    """Account requires a password change in the EMMASA web portal."""


class EmmasaConnectionError(EmmasaApiError):
    """Network, timeout, or upstream server error."""


class EmmasaQueryError(EmmasaApiError):
    """Error executing or parsing a Grafana datasource query."""
