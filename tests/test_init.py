"""Tests for EMMASA integration lifecycle (__init__.py)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emmasa.api import (
    EmmasaAuthError,
    EmmasaConnectionError,
    TokenBundle,
)
from custom_components.emmasa.const import CONF_REFRESH_TOKEN, DOMAIN


async def test_setup_unload_and_token_persistence(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    mock_recorder_stats: dict[str, Any],
) -> None:
    """Setup stores runtime_data, persists rotated refresh_token, and unloads cleanly."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data.client is mock_emmasa_client

    # Simulate token rotation callback passed into EmmasaApiClient
    on_tokens_updated = mock_emmasa_client._init_cls.call_args.kwargs["on_tokens_updated"]
    await on_tokens_updated(
        TokenBundle(
            access_token="new-jwt",
            refresh_token="new-rotated-rt",
            expires_at=time.time() + 7200,
        )
    )
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "new-rotated-rt"

    # Identical refresh token is a no-op
    await on_tokens_updated(
        TokenBundle(
            access_token="new-jwt-2",
            refresh_token="new-rotated-rt",
            expires_at=time.time() + 7200,
        )
    )

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    mock_emmasa_client.async_logout.assert_awaited_once()


async def test_setup_auth_error_triggers_reauth(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
) -> None:
    """EmmasaAuthError during setup raises ConfigEntryAuthFailed and starts reauth."""
    mock_emmasa_client.async_ensure_authenticated.side_effect = EmmasaAuthError("expired")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"].get("source") == "reauth" for flow in flows)


async def test_setup_connection_error_retries(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
) -> None:
    """EmmasaConnectionError during setup marks entry as SETUP_RETRY."""
    mock_emmasa_client.async_ensure_authenticated.side_effect = EmmasaConnectionError("timeout")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
