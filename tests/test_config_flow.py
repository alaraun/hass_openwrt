"""Config, reauth and reconfigure flows."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from .conftest import DOMAIN, PASSWORD, USERNAME, RpcError


def _user_input(router, **overrides):
    return {
        "id": "test-ap",
        "address": "127.0.0.1",
        "username": USERNAME,
        "password": PASSWORD,
        "https": False,
        "verify_cert": False,
        "port": router.port,
        "path": "/ubus",
        "interval": 30,
        "wps": False,
        **overrides,
    }


async def test_user_flow_creates_entry(hass, router):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(router)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "test-ap - 127.0.0.1"
    assert result["result"].unique_id == "127.0.0.1"


async def test_user_flow_errors(hass, router):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(router, password="wrong")
    )
    assert result["errors"] == {"base": "invalid_auth"}

    router.answers[("session", "login")] = RpcError(-32000, "Object not found")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(router)
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_aborts_for_a_configured_address(hass, router, make_entry):
    make_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(router)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_the_password(hass, router, make_entry):
    router.password = "new-password"
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flows = list(entry.async_get_active_flows(hass, {config_entries.SOURCE_REAUTH}))
    assert flows

    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {"username": USERNAME, "password": "still-wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {"username": USERNAME, "password": "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-password"
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_reconfigure_changes_the_interval(hass, router, make_entry):
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"username": USERNAME, "password": PASSWORD, "https": False, "verify_cert": False,
         "port": router.port, "path": "/ubus", "interval": 60, "wps": False},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["interval"] == 60
    assert entry.data["id"] == "test-ap"
