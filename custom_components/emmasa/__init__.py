"""The EMMASA (Portal Ciudadano) integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    EmmasaApiClient,
    EmmasaAuthError,
    EmmasaConnectionError,
    TokenBundle,
)
from .const import CONF_REFRESH_TOKEN, DOMAIN
from .coordinator import EmmasaDataUpdateCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


@dataclass(slots=True)
class EmmasaRuntimeData:
    """Runtime data stored in ConfigEntry.runtime_data."""

    client: EmmasaApiClient
    coordinator: EmmasaDataUpdateCoordinator


type EmmasaConfigEntry = ConfigEntry[EmmasaRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: EmmasaConfigEntry) -> bool:
    """Set up EMMASA from a config entry."""
    session = async_get_clientsession(hass)

    async def _async_persist_tokens(tokens: TokenBundle) -> None:
        if tokens.refresh_token and tokens.refresh_token != entry.data.get(CONF_REFRESH_TOKEN):
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_REFRESH_TOKEN: tokens.refresh_token},
            )

    client = EmmasaApiClient(
        session=session,
        username=str(entry.data[CONF_USERNAME]),
        password=str(entry.data[CONF_PASSWORD]),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        on_tokens_updated=_async_persist_tokens,
    )

    try:
        await client.async_ensure_authenticated()
    except EmmasaAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="auth_failed",
        ) from err
    except EmmasaConnectionError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
        ) from err

    coordinator = EmmasaDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = EmmasaRuntimeData(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EmmasaConfigEntry) -> bool:
    """Unload an EMMASA config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.client.async_logout()
    return unload_ok
