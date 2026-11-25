"""Config flow for EMMASA (Portal Ciudadano)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    EmmasaApiClient,
    EmmasaAuthError,
    EmmasaConnectionError,
    EmmasaMfaRequiredError,
    EmmasaPasswordChangeRequiredError,
    TokenBundle,
    UserProfile,
)
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_HOURS,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    MAX_SCAN_INTERVAL_HOURS,
    MIN_BACKFILL_DAYS,
    MIN_SCAN_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)
_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)


def _normalize_username(username: str) -> str:
    """Normalize DNI/NIF login identifier matching EMMASA's client normalization."""
    return "".join(username.strip().split()).upper()


class EmmasaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EMMASA (Portal Ciudadano)."""

    VERSION = 1

    async def _async_validate_credentials(
        self,
        username: str,
        password: str,
    ) -> tuple[UserProfile, TokenBundle, int]:
        """Validate credentials against Acuama IdP and Portal Ciudadano BFF."""
        session = async_get_clientsession(self.hass)
        client = EmmasaApiClient(session=session, username=username, password=password)
        tokens = await client.async_authenticate()
        profile = await client.async_get_profile()
        contracts = await client.async_get_contracts()
        await client.async_logout()
        return profile, tokens, len(contracts)

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = _normalize_username(str(user_input[CONF_USERNAME]))
            password = str(user_input[CONF_PASSWORD])
            try:
                profile, tokens, contract_count = await self._async_validate_credentials(
                    username,
                    password,
                )
            except EmmasaMfaRequiredError:
                errors["base"] = "mfa_required"
            except EmmasaPasswordChangeRequiredError:
                errors["base"] = "password_change_required"
            except EmmasaAuthError:
                errors["base"] = "invalid_auth"
            except EmmasaConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating EMMASA credentials")
                errors["base"] = "unknown"
            else:
                if contract_count == 0:
                    errors["base"] = "no_contracts"
                else:
                    unique_id = _normalize_username(profile.login)
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()
                    data: dict[str, Any] = {
                        CONF_USERNAME: unique_id,
                        CONF_PASSWORD: password,
                    }
                    if tokens.refresh_token:
                        data[CONF_REFRESH_TOKEN] = tokens.refresh_token
                    return self.async_create_entry(
                        title=f"EMMASA ({unique_id})",
                        data=data,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Start reauthentication when credentials or tokens expire."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm updated password during reauthentication."""
        reauth_entry = self._get_reauth_entry()
        username = str(reauth_entry.data[CONF_USERNAME])
        errors: dict[str, str] = {}

        if user_input is not None:
            password = str(user_input[CONF_PASSWORD])
            try:
                _, tokens, _ = await self._async_validate_credentials(username, password)
            except EmmasaMfaRequiredError:
                errors["base"] = "mfa_required"
            except EmmasaPasswordChangeRequiredError:
                errors["base"] = "password_change_required"
            except EmmasaAuthError:
                errors["base"] = "invalid_auth"
            except EmmasaConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during EMMASA reauthentication")
                errors["base"] = "unknown"
            else:
                updates: dict[str, Any] = {CONF_PASSWORD: password}
                if tokens.refresh_token:
                    updates[CONF_REFRESH_TOKEN] = tokens.refresh_token
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates=updates,
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR}),
            description_placeholders={"username": username},
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Allow reconfiguring credentials without deleting the config entry."""
        reconfigure_entry = self._get_reconfigure_entry()
        current_username = str(reconfigure_entry.data[CONF_USERNAME])
        errors: dict[str, str] = {}

        if user_input is not None:
            username = _normalize_username(str(user_input[CONF_USERNAME]))
            password = str(user_input[CONF_PASSWORD])
            try:
                profile, tokens, contract_count = await self._async_validate_credentials(
                    username,
                    password,
                )
            except EmmasaMfaRequiredError:
                errors["base"] = "mfa_required"
            except EmmasaPasswordChangeRequiredError:
                errors["base"] = "password_change_required"
            except EmmasaAuthError:
                errors["base"] = "invalid_auth"
            except EmmasaConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during EMMASA reconfiguration")
                errors["base"] = "unknown"
            else:
                if contract_count == 0:
                    errors["base"] = "no_contracts"
                else:
                    unique_id = _normalize_username(profile.login)
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_mismatch()
                    updates: dict[str, Any] = {
                        CONF_USERNAME: unique_id,
                        CONF_PASSWORD: password,
                    }
                    if tokens.refresh_token:
                        updates[CONF_REFRESH_TOKEN] = tokens.refresh_token
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        data_updates=updates,
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=current_username): str,
                    vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EmmasaOptionsFlow:
        """Return the options flow handler."""
        return EmmasaOptionsFlow()


class EmmasaOptionsFlow(OptionsFlowWithReload):
    """Handle options flow for EMMASA with automatic entry reload."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage polling interval and historical backfill options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_scan = int(
            self.config_entry.options.get(CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS)
        )
        current_backfill = int(
            self.config_entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
        )

        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL_HOURS, default=current_scan): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SCAN_INTERVAL_HOURS, max=MAX_SCAN_INTERVAL_HOURS),
                ),
                vol.Required(CONF_BACKFILL_DAYS, default=current_backfill): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_BACKFILL_DAYS, max=MAX_BACKFILL_DAYS),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
