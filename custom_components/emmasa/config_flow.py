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
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    ContractInfo,
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
    CONF_CONTRACT_ID,
    CONF_CONTRACT_NAME,
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
_USERNAME_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
)
_PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): _USERNAME_SELECTOR,
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)


def _normalize_username(username: str) -> str:
    """Normalize DNI/NIF login identifier matching EMMASA's client normalization."""
    return "".join(username.strip().split()).upper()


def _format_contract_option(contract: ContractInfo) -> SelectOptionDict:
    """Format a supply contract option label for the selection dropdown."""
    suffix = " (Telelectura)" if contract.allows_alert_configuration else ""
    return SelectOptionDict(
        value=contract.contract_id,
        label=f"Contrato {contract.name}{suffix}",
    )


class EmmasaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EMMASA (Portal Ciudadano)."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending_username: str | None = None
        self._pending_password: str | None = None
        self._pending_refresh_token: str | None = None
        self._pending_contracts: list[ContractInfo] = []

    async def _async_validate_credentials(
        self,
        username: str,
        password: str,
    ) -> tuple[UserProfile, TokenBundle, list[ContractInfo]]:
        """Validate credentials against Acuama IdP and Portal Ciudadano BFF."""
        if not username or not password:
            raise EmmasaAuthError("Username and password must not be empty")
        session = async_get_clientsession(self.hass)
        client = EmmasaApiClient(session=session, username=username, password=password)
        tokens = await client.async_authenticate()
        profile = await client.async_get_profile()
        contracts = await client.async_get_contracts()
        if not contracts:
            await client.async_logout()
        return profile, tokens, contracts

    async def _async_create_contract_entry(
        self,
        username: str,
        password: str,
        refresh_token: str | None,
        contract: ContractInfo,
    ) -> ConfigFlowResult:
        """Create a config entry scoped to a single selected water supply contract."""
        await self.async_set_unique_id(contract.contract_id)
        self._abort_if_unique_id_configured()

        data: dict[str, Any] = {
            CONF_USERNAME: username,
            CONF_PASSWORD: password,
            CONF_CONTRACT_ID: contract.contract_id,
            CONF_CONTRACT_NAME: contract.name,
        }
        if refresh_token:
            data[CONF_REFRESH_TOKEN] = refresh_token

        return self.async_create_entry(
            title=f"EMMASA ({contract.name})",
            data=data,
        )

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial credentials step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = _normalize_username(str(user_input[CONF_USERNAME]))
            password = str(user_input[CONF_PASSWORD])
            try:
                profile, tokens, contracts = await self._async_validate_credentials(
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
                if not contracts:
                    errors["base"] = "no_contracts"
                else:
                    normalized_login = _normalize_username(profile.login)
                    if len(contracts) == 1:
                        return await self._async_create_contract_entry(
                            normalized_login,
                            password,
                            tokens.refresh_token,
                            contracts[0],
                        )
                    self._pending_username = normalized_login
                    self._pending_password = password
                    self._pending_refresh_token = tokens.refresh_token
                    self._pending_contracts = contracts
                    return await self.async_step_select_contract()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_select_contract(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Prompt user to pick one water supply contract when an account has multiple."""
        errors: dict[str, str] = {}
        contracts_by_id = {c.contract_id: c for c in self._pending_contracts}

        if user_input is not None:
            selected_id = str(user_input[CONF_CONTRACT_ID])
            selected_contract = contracts_by_id.get(selected_id)
            if selected_contract is None:
                errors["base"] = "invalid_contract"
            else:
                username = self._pending_username or ""
                password = self._pending_password or ""
                refresh_token = self._pending_refresh_token
                self._pending_password = None
                self._pending_refresh_token = None
                return await self._async_create_contract_entry(
                    username,
                    password,
                    refresh_token,
                    selected_contract,
                )

        options = [_format_contract_option(c) for c in self._pending_contracts]
        schema = vol.Schema(
            {
                vol.Required(CONF_CONTRACT_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="select_contract",
            data_schema=schema,
            description_placeholders={"count": str(len(self._pending_contracts))},
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
        target_contract_id = reauth_entry.data.get(CONF_CONTRACT_ID)
        errors: dict[str, str] = {}

        if user_input is not None:
            password = str(user_input[CONF_PASSWORD])
            try:
                _, tokens, contracts = await self._async_validate_credentials(username, password)
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
                matched_contract = (
                    next((c for c in contracts if c.contract_id == target_contract_id), None)
                    if target_contract_id
                    else None
                )
                if target_contract_id and matched_contract is None:
                    errors["base"] = "contract_not_found"
                else:
                    new_data: dict[str, Any] = {
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    }
                    if matched_contract is not None:
                        new_data[CONF_CONTRACT_ID] = matched_contract.contract_id
                        new_data[CONF_CONTRACT_NAME] = matched_contract.name
                    if tokens.refresh_token:
                        new_data[CONF_REFRESH_TOKEN] = tokens.refresh_token
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data=new_data,
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
        target_contract_id = reconfigure_entry.data.get(CONF_CONTRACT_ID)
        errors: dict[str, str] = {}

        if user_input is not None:
            username = _normalize_username(str(user_input[CONF_USERNAME]))
            password = str(user_input[CONF_PASSWORD])
            try:
                profile, tokens, contracts = await self._async_validate_credentials(
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
                if not contracts:
                    errors["base"] = "no_contracts"
                else:
                    normalized_login = _normalize_username(profile.login)
                    new_data: dict[str, Any] = {
                        CONF_USERNAME: normalized_login,
                        CONF_PASSWORD: password,
                    }
                    if target_contract_id:
                        matched = next(
                            (c for c in contracts if c.contract_id == target_contract_id),
                            None,
                        )
                        if matched is None:
                            return self.async_abort(reason="unique_id_mismatch")
                        await self.async_set_unique_id(matched.contract_id)
                        self._abort_if_unique_id_mismatch()
                        new_data[CONF_CONTRACT_ID] = matched.contract_id
                        new_data[CONF_CONTRACT_NAME] = matched.name
                    else:
                        await self.async_set_unique_id(normalized_login)
                        self._abort_if_unique_id_mismatch()
                    if tokens.refresh_token:
                        new_data[CONF_REFRESH_TOKEN] = tokens.refresh_token
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        data=new_data,
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=current_username): _USERNAME_SELECTOR,
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
