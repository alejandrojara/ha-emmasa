"""Constants for the EMMASA (Portal Ciudadano) integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from .api.const import (
    ACUAMA_API_BASE,
    API_VERSION,
    DEFAULT_GRAFANA_DATASOURCE_TYPE,
    DEFAULT_GRAFANA_DATASOURCE_UID,
    EMMASA_TIMEZONE,
    EXPLOTACION_CODE,
    GRAFANA_BASE,
    GRAFANA_CONSUMOS_DASHBOARD_UID,
    GRAFANA_ORG_ID,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    PORTAL_API_BASE,
    PORTAL_BASE,
    PORTAL_DASHBOARD_URL,
)

DOMAIN: Final = "emmasa"

__all__ = [
    "ACUAMA_API_BASE",
    "ALERT_DAYTIME_ID",
    "ALERT_NIGHTTIME_ID",
    "API_VERSION",
    "CONF_BACKFILL_DAYS",
    "CONF_CONTRACT_ID",
    "CONF_CONTRACT_NAME",
    "CONF_REFRESH_TOKEN",
    "CONF_SCAN_INTERVAL_HOURS",
    "DEFAULT_BACKFILL_DAYS",
    "DEFAULT_GRAFANA_DATASOURCE_TYPE",
    "DEFAULT_GRAFANA_DATASOURCE_UID",
    "DEFAULT_SCAN_INTERVAL",
    "DEFAULT_SCAN_INTERVAL_HOURS",
    "DOMAIN",
    "EMMASA_TIMEZONE",
    "EXPLOTACION_CODE",
    "GRAFANA_BASE",
    "GRAFANA_CONSUMOS_DASHBOARD_UID",
    "GRAFANA_ORG_ID",
    "MAX_BACKFILL_DAYS",
    "MAX_SCAN_INTERVAL_HOURS",
    "MIN_BACKFILL_DAYS",
    "MIN_SCAN_INTERVAL_HOURS",
    "OAUTH_CLIENT_ID",
    "OAUTH_REDIRECT_URI",
    "OAUTH_SCOPE",
    "PORTAL_API_BASE",
    "PORTAL_BASE",
    "PORTAL_DASHBOARD_URL",
    "TO_REDACT",
]

# Config & options keys
CONF_CONTRACT_ID: Final = "contract_id"
CONF_CONTRACT_NAME: Final = "contract_name"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_BACKFILL_DAYS: Final = "backfill_days"
CONF_SCAN_INTERVAL_HOURS: Final = "scan_interval_hours"

# Defaults & bounds
DEFAULT_BACKFILL_DAYS: Final = 30
MIN_BACKFILL_DAYS: Final = 7
MAX_BACKFILL_DAYS: Final = 365

DEFAULT_SCAN_INTERVAL_HOURS: Final = 2
MIN_SCAN_INTERVAL_HOURS: Final = 1
MAX_SCAN_INTERVAL_HOURS: Final = 6
DEFAULT_SCAN_INTERVAL: Final = timedelta(hours=DEFAULT_SCAN_INTERVAL_HOURS)

# Alert type identifiers observed in EMMASA Portal Ciudadano
ALERT_DAYTIME_ID: Final = "1986c39c-b7f3-4d10-bd6b-1464dd50b760"
ALERT_NIGHTTIME_ID: Final = "69f10dbc-765d-4025-8b31-c010b114aa76"

# Redaction keys for diagnostics
TO_REDACT: Final = {
    "username",
    "password",
    "login",
    "email",
    "access_token",
    "refresh_token",
    "id_token",
    "grafana_session",
    "contract_id",
    "contract_name",
    "id",
    "name",
    "serial_number",
    "statistic_id",
}
