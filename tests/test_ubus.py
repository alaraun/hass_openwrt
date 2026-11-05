"""The ubus client's error classes and logging."""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant

from custom_components.openwrt.ubus import (
    Ubus,
    UbusAuthError,
    UbusConnectionError,
    UbusPermissionError,
    UbusRPCError,
)

from .conftest import PASSWORD, SESSION, USERNAME, FakeRouter, RpcError, Stall, Status


def _client(hass: HomeAssistant, router: FakeRouter, **kwargs) -> Ubus:
    return Ubus(hass, router.url, USERNAME, kwargs.pop("password", PASSWORD), **kwargs)


async def test_call_logs_in_and_returns_the_result(hass, router):
    ubus = _client(hass, router)
    info = await ubus.api_call("system", "info", {})
    assert info["uptime"] == 1000
    assert router.logins == 1
    assert ubus.acls["ubus"]["system"] == ["board", "info", "reboot"]


async def test_timeout_is_a_connection_error_with_a_message(hass, router):
    """aiohttp's total timeout raises a bare TimeoutError; the client names the call."""
    router.answers[("system", "info")] = Stall()
    ubus = _client(hass, router, timeout=0.2)
    with pytest.raises(UbusConnectionError, match=r"system\.info: no answer within 0\.2 s"):
        await ubus.api_call("system", "info", {})


async def test_connect_timeout_is_named(hass, router, monkeypatch):
    """A router that is off fails the connect bound, not the answer bound."""
    import aiohttp

    from custom_components.openwrt import ubus as ubus_module

    seen = {}

    class _Session:
        def post(self, url, json=None, timeout=None):
            seen["timeout"] = timeout
            raise aiohttp.ConnectionTimeoutError("Connection timeout to host")

    monkeypatch.setattr(ubus_module.Ubus, "_session", property(lambda self: _Session()))
    ubus = _client(hass, router)
    with pytest.raises(UbusConnectionError, match=r"session\.login: no connection to .* within 10 s"):
        await ubus.login()
    assert seen["timeout"].total == 30
    assert seen["timeout"].sock_connect == 10


async def test_malformed_reply_is_a_connection_error(hass, router):
    router.answers[("system", "info")] = b'{"jsonrpc": "2.0", "result": [0, {"upt'
    ubus = _client(hass, router)
    with pytest.raises(UbusConnectionError, match="malformed reply"):
        await ubus.api_call("system", "info", {})


async def test_unreachable_endpoint_is_a_connection_error(hass, router):
    ubus = Ubus(hass, "http://127.0.0.1:1/ubus", USERNAME, PASSWORD)
    with pytest.raises(UbusConnectionError, match="session.login"):
        await ubus.api_call("system", "info", {})


async def test_rejected_password_is_an_auth_error(hass, router):
    ubus = _client(hass, router, password="wrong")
    with pytest.raises(UbusAuthError):
        await ubus.login()


async def test_login_without_session_object_is_not_an_auth_error(hass, router):
    """While rpcd starts, the session object is missing: retry later, no reauth."""
    router.answers[("session", "login")] = RpcError(-32000, "Object not found")
    ubus = _client(hass, router)
    with pytest.raises(UbusConnectionError):
        await ubus.login()


async def test_stale_session_logs_in_again(hass, router):
    ubus = _client(hass, router)
    await ubus.api_call("system", "info", {})
    router.session_valid = False
    assert (await ubus.api_call("system", "info", {}))["uptime"] == 1000
    assert router.logins == 2


async def test_denied_after_fresh_login_is_a_permission_error(hass, router):
    router.answers[("system", "info")] = RpcError(-32002, "Access denied")
    ubus = _client(hass, router)
    with pytest.raises(UbusPermissionError):
        await ubus.api_call("system", "info", {})
    assert isinstance(UbusPermissionError(), UbusRPCError)


async def test_missing_object_returns_empty(hass, router):
    ubus = _client(hass, router)
    assert await ubus.api_call("mwan3", "status", {}) == {}


async def test_ubus_status_is_an_rpc_error(hass, router):
    router.answers[("system", "info")] = Status(2)
    ubus = _client(hass, router)
    with pytest.raises(UbusRPCError, match="ubus status 2"):
        await ubus.api_call("system", "info", {})


async def test_debug_log_holds_no_password_or_session(hass, router, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.openwrt")
    ubus = _client(hass, router)
    await ubus.api_call("system", "info", {})
    assert "system.info" in caplog.text
    assert PASSWORD not in caplog.text
    assert SESSION not in caplog.text
