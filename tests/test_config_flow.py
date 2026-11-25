"""Tests for EMMASA config, reauth, reconfigure, and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.recorder import Recorder
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emmasa.api import (
    EmmasaAuthError,
    EmmasaConnectionError,
    EmmasaMfaRequiredError,
    EmmasaPasswordChangeRequiredError,
    UserProfile,
)
from custom_components.emmasa.const import (
    CONF_BACKFILL_DAYS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_HOURS,
    DOMAIN,
)

from .conftest import MOCK_PASSWORD, MOCK_USERNAME


async def test_user_flow_success(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_emmasa_client: MagicMock,
) -> None:
    """Test successful initial user setup with DNI normalization and unique_id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch("custom_components.emmasa.async_setup_entry", return_value=True) as mock_setup:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: " 12345678 z ", CONF_PASSWORD: MOCK_PASSWORD},
        )
        await hass.async_block_till_done()

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == f"EMMASA ({MOCK_USERNAME})"
    assert result2["data"][CONF_USERNAME] == MOCK_USERNAME
    assert result2["data"][CONF_PASSWORD] == MOCK_PASSWORD
    assert result2["data"][CONF_REFRESH_TOKEN] == "rotated-refresh-token"
    assert len(mock_setup.mock_calls) == 1


async def test_user_flow_duplicate_abort(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
) -> None:
    """Duplicate account aborts with already_configured."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: MOCK_USERNAME, CONF_PASSWORD: MOCK_PASSWORD},
    )
    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("side_effect", "expected_error"),
    [
        (EmmasaAuthError("bad"), "invalid_auth"),
        (EmmasaMfaRequiredError("mfa"), "mfa_required"),
        (EmmasaPasswordChangeRequiredError("pwd"), "password_change_required"),
        (EmmasaConnectionError("net"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_and_recovery(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_emmasa_client: MagicMock,
    side_effect: Exception,
    expected_error: str,
) -> None:
    """User flow surfaces each error key cleanly and recovers on retry."""
    mock_emmasa_client.async_authenticate.side_effect = side_effect
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: MOCK_USERNAME, CONF_PASSWORD: "wrong"},
    )
    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": expected_error}

    # Recover cleanly on retry
    mock_emmasa_client.async_authenticate.side_effect = None
    with patch("custom_components.emmasa.async_setup_entry", return_value=True):
        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {CONF_USERNAME: MOCK_USERNAME, CONF_PASSWORD: MOCK_PASSWORD},
        )
    assert result3["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_no_contracts(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_emmasa_client: MagicMock,
) -> None:
    """Account with zero contracts shows no_contracts error."""
    mock_emmasa_client.async_get_contracts.return_value = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: MOCK_USERNAME, CONF_PASSWORD: MOCK_PASSWORD},
    )
    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": "no_contracts"}


async def test_reauth_flow_success_and_errors(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
) -> None:
    """Test reauthentication flow error handling and credential update."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    for exc, err_key in (
        (EmmasaAuthError("bad"), "invalid_auth"),
        (EmmasaMfaRequiredError("mfa"), "mfa_required"),
        (EmmasaPasswordChangeRequiredError("pwd"), "password_change_required"),
        (EmmasaConnectionError("net"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ):
        mock_emmasa_client.async_authenticate.side_effect = exc
        err_res = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "bad"},
        )
        assert err_res["type"] is FlowResultType.FORM
        assert err_res["errors"] == {"base": err_key}

    mock_emmasa_client.async_authenticate.side_effect = None
    with patch("custom_components.emmasa.async_setup_entry", return_value=True):
        ok_res = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "new-password"},
        )
        await hass.async_block_till_done()

    assert ok_res["type"] is FlowResultType.ABORT
    assert ok_res["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-password"


async def test_reconfigure_flow_success_errors_and_mismatch(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
) -> None:
    """Test reconfigure flow updates credentials, validates errors, and guards unique_id."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    for exc, err_key in (
        (EmmasaAuthError("bad"), "invalid_auth"),
        (EmmasaMfaRequiredError("mfa"), "mfa_required"),
        (EmmasaPasswordChangeRequiredError("pwd"), "password_change_required"),
        (EmmasaConnectionError("net"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ):
        mock_emmasa_client.async_authenticate.side_effect = exc
        err_res = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: MOCK_USERNAME, CONF_PASSWORD: "bad"},
        )
        assert err_res["type"] is FlowResultType.FORM
        assert err_res["errors"] == {"base": err_key}

    # No contracts check
    mock_emmasa_client.async_authenticate.side_effect = None
    mock_emmasa_client.async_get_contracts.return_value = []
    no_c = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: MOCK_USERNAME, CONF_PASSWORD: "new-password"},
    )
    assert no_c["type"] is FlowResultType.FORM
    assert no_c["errors"] == {"base": "no_contracts"}

    # Success
    mock_emmasa_client.async_get_contracts.return_value = [MagicMock()]
    with patch("custom_components.emmasa.async_setup_entry", return_value=True):
        ok_res = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: MOCK_USERNAME, CONF_PASSWORD: "updated-password"},
        )
        await hass.async_block_till_done()

    assert ok_res["type"] is FlowResultType.ABORT
    assert ok_res["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "updated-password"

    # Mismatched account aborts cleanly
    mock_emmasa_client.async_get_profile = AsyncMock(
        return_value=UserProfile(username="Other", login="99999999A")
    )
    res_mismatch = await mock_config_entry.start_reconfigure_flow(hass)
    res_abort = await hass.config_entries.flow.async_configure(
        res_mismatch["flow_id"],
        {CONF_USERNAME: "99999999A", CONF_PASSWORD: "other-password"},
    )
    assert res_abort["type"] is FlowResultType.ABORT
    assert res_abort["reason"] == "unique_id_mismatch"


async def test_options_flow(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    mock_recorder_stats: dict,
) -> None:
    """OptionsFlowWithReload persists polling interval and backfill days."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_SCAN_INTERVAL_HOURS: 4, CONF_BACKFILL_DAYS: 60},
    )
    await hass.async_block_till_done()

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        CONF_SCAN_INTERVAL_HOURS: 4,
        CONF_BACKFILL_DAYS: 60,
    }
