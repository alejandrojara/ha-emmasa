"""Typed data models for the EMMASA API client."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True, frozen=True)
class TokenBundle:
    """OAuth2 token bundle returned by the Sacyr Acuama IdP."""

    access_token: str
    refresh_token: str | None
    expires_at: float
    id_token: str | None = None

    def is_expired(self, margin_seconds: float = 60.0) -> bool:
        """Return True when the access token is expired or within the safety margin."""
        return time.time() >= (self.expires_at - margin_seconds)


@dataclass(slots=True, frozen=True)
class GrafanaSession:
    """Short-lived Grafana session credentials issued by the Portal Ciudadano BFF."""

    session_token: str
    expires_at: float

    def is_expired(self, margin_seconds: float = 60.0) -> bool:
        """Return True when the Grafana session cookie is expired or near expiry."""
        return time.time() >= (self.expires_at - margin_seconds)


@dataclass(slots=True, frozen=True)
class UserProfile:
    """Authenticated Portal Ciudadano user profile (/api/auth/me)."""

    username: str
    login: str
    email: str | None = None


@dataclass(slots=True, frozen=True)
class ContractInfo:
    """Water supply contract returned by /api/contract/advanced-search."""

    contract_id: str
    name: str
    allows_alert_configuration: bool


@dataclass(slots=True, frozen=True)
class ContractAlert:
    """Configurable consumption alert returned by /api/contract-alerts/{contractId}."""

    alert_id: str
    alert_name: str
    alert_description: str
    config_id: str | None
    user_limit_liters: float | None
    is_active: bool
    allows_alert_configuration: bool


@dataclass(slots=True, frozen=True)
class HourlyReading:
    """Single hourly water consumption sample from ts_telelectura."""

    timestamp_utc: datetime
    volume_m3: float


@dataclass(slots=True, frozen=True)
class MeterReading:
    """Cumulative water meter reading (vol_acc) in cubic meters."""

    timestamp_utc: datetime
    volume_acc_m3: float


@dataclass(slots=True, frozen=True)
class PeriodConsumption:
    """Unbilled consumption accrued since the end of the last billed period."""

    since_utc: datetime | None
    volume_m3: float


@dataclass(slots=True, frozen=True)
class BilledConsumption:
    """Most recent officially billed bimonthly consumption."""

    period_code: int
    period_label: str
    volume_m3: float


@dataclass(slots=True, frozen=True)
class InvoiceSummary:
    """Most recent invoice amount and billing period."""

    period_code: str
    amount_eur: float
    invoice_date_utc: datetime | None


@dataclass(slots=True, frozen=True)
class ContractSnapshot:
    """Aggregated telemetry, billing, and alerts snapshot for one contract."""

    contract: ContractInfo
    hourly_readings: tuple[HourlyReading, ...]
    latest_meter_reading: MeterReading | None
    current_period_consumption: PeriodConsumption | None
    yesterday_consumption_liters: float | None
    latest_billed_consumption: BilledConsumption | None
    latest_invoice: InvoiceSummary | None
    alerts: tuple[ContractAlert, ...]
    last_reading_timestamp_utc: datetime | None
