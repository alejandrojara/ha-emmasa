"""Tests for EMMASA diagnostics redaction."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emmasa.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    MOCK_CONTRACT_NAME,
    MOCK_CONTRACT_UUID,
    MOCK_PASSWORD,
    MOCK_USERNAME,
)


async def test_diagnostics_redacts_all_pii_and_secrets(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    mock_recorder_stats: dict[str, Any],
) -> None:
    """Diagnostics redact credentials, tokens, and contract identifiers while preserving metrics."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    dumped = json.dumps(diag)

    # Secrets and PII must never leak into diagnostics output
    assert MOCK_USERNAME not in dumped
    assert MOCK_PASSWORD not in dumped
    assert "initial-refresh-token" not in dumped
    assert "rotated-refresh-token" not in dumped
    assert "mock-grafana-cookie" not in dumped
    assert MOCK_CONTRACT_UUID not in dumped
    assert MOCK_CONTRACT_NAME not in dumped

    assert diag["entry"]["data"]["username"] == REDACTED
    assert diag["entry"]["data"]["password"] == REDACTED
    assert diag["entry"]["data"]["contract_id"] == REDACTED
    assert diag["entry"]["data"]["contract_name"] == REDACTED
    assert diag["entry"]["data"]["refresh_token"] == REDACTED
    assert diag["coordinator"]["contracts_count"] == 1
    assert diag["coordinator"]["contracts"][0]["contract_id"] == REDACTED
    assert diag["coordinator"]["contracts"][0]["name"] == REDACTED
    assert diag["coordinator"]["contracts"][0]["statistic_id"] == REDACTED
    assert diag["coordinator"]["contracts"][0]["hourly_readings_in_window"] == 3
    assert diag["coordinator"]["contracts"][0]["imported_statistics_last_sync"] == 3
    assert diag["client"]["datasource_uid"] == "dfn7f7nyfsuf4a"
