"""Decoupled asynchronous API client for EMMASA Portal Ciudadano (Elliot Cloud)."""

from __future__ import annotations

from .client import EmmasaApiClient
from .exceptions import (
    EmmasaApiError,
    EmmasaAuthError,
    EmmasaConnectionError,
    EmmasaMfaRequiredError,
    EmmasaPasswordChangeRequiredError,
    EmmasaQueryError,
)
from .models import (
    BilledConsumption,
    ContractAlert,
    ContractInfo,
    ContractSnapshot,
    GrafanaSession,
    HourlyReading,
    InvoiceSummary,
    MeterReading,
    PeriodConsumption,
    TokenBundle,
    UserProfile,
)

__all__ = [
    "BilledConsumption",
    "ContractAlert",
    "ContractInfo",
    "ContractSnapshot",
    "EmmasaApiClient",
    "EmmasaApiError",
    "EmmasaAuthError",
    "EmmasaConnectionError",
    "EmmasaMfaRequiredError",
    "EmmasaPasswordChangeRequiredError",
    "EmmasaQueryError",
    "GrafanaSession",
    "HourlyReading",
    "InvoiceSummary",
    "MeterReading",
    "PeriodConsumption",
    "TokenBundle",
    "UserProfile",
]
