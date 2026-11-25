"""Constants for the decoupled EMMASA API client."""

from __future__ import annotations

from typing import Final

# API Endpoints
ACUAMA_API_BASE: Final = "https://acuamaemmasa.sacyr.com/webapi/api"
PORTAL_BASE: Final = "https://portalciudadano.emmasa.elliotcloud.com"
PORTAL_API_BASE: Final = f"{PORTAL_BASE}/api"
GRAFANA_BASE: Final = f"{PORTAL_BASE}/grafana"
PORTAL_DASHBOARD_URL: Final = f"{PORTAL_BASE}/dashboard/consumos/consumos-home"

# OAuth2 PKCE parameters (Portal Ciudadano SPA)
OAUTH_CLIENT_ID: Final = "F17573F7-2BF9-4A27-A011-3FA25F12CB77"
OAUTH_REDIRECT_URI: Final = f"{PORTAL_BASE}/callback"
OAUTH_SCOPE: Final = "openid profile email"
EXPLOTACION_CODE: Final = "Emmasa"
API_VERSION: Final = "0.1"

# Grafana metadata
GRAFANA_CONSUMOS_DASHBOARD_UID: Final = "feu6mm4xbzncwd"
DEFAULT_GRAFANA_DATASOURCE_UID: Final = "dfn7f7nyfsuf4a"
DEFAULT_GRAFANA_DATASOURCE_TYPE: Final = "grafana-postgresql-datasource"
GRAFANA_ORG_ID: Final = "1"

# Timezone used by EMMASA Santa Cruz de Tenerife
EMMASA_TIMEZONE: Final = "Atlantic/Canary"
