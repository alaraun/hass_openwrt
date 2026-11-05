"""Fixtures: a fake OpenWrt ubus endpoint served over HTTP on localhost."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from typing import Any

import pytest
from aiohttp import web
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Import before Home Assistant puts its testing config dir on sys.path, so that
# `custom_components` resolves to this repository.
import custom_components.openwrt  # noqa: F401

DOMAIN = "openwrt"
USERNAME = "hass"
PASSWORD = "test-password"
SESSION = "c0ffee00c0ffee00c0ffee00c0ffee00"

CLIENT_MAC = "02:00:00:00:00:01"
HOST_IP = "192.0.2.10"

UCI_WIRELESS = {
    "values": {
        "radio0": {".type": "wifi-device", ".name": "radio0", ".index": 0},
        "default_radio0": {
            ".type": "wifi-iface",
            ".name": "default_radio0",
            ".index": 1,
            "device": "radio0",
            "mode": "ap",
            "ssid": "TestNet",
            "network": "lan",
        },
    }
}


def default_answers() -> dict[tuple[str, str], Any]:
    return {
        ("system", "info"): {
            "uptime": 1000,
            "localtime": 1_700_000_000,
            "load": [32768, 16384, 8192],
            "memory": {"total": 512 * 1024 * 1024, "free": 256 * 1024 * 1024},
            "root": {"total": 1000, "free": 900, "used": 100, "avail": 900},
            "tmp": {"total": 1000, "free": 990, "used": 10, "avail": 990},
            "swap": {"total": 0, "free": 0},
        },
        ("system", "board"): {
            "model": "Test Router",
            "release": {"distribution": "OpenWrt", "version": "25.12.0", "revision": "r1"},
        },
        # rpcd on OpenWrt 25.12 answers this with ubus status 2
        ("network.wireless", "status"): Status(2),
        ("uci", "get"): UCI_WIRELESS,
        ("hostapd.phy0-ap0", "get_clients"): {"clients": {CLIENT_MAC: {"signal": -50}}},
        ("luci-rpc", "getHostHints"): {
            CLIENT_MAC.upper(): {"ipaddrs": [HOST_IP], "name": "laptop"}
        },
        ("file", "read"): Status(4),
    }


DEFAULT_ACLS = {
    "ubus": {
        "system": ["board", "info", "reboot"],
        "network.wireless": ["status"],
        "uci": ["get"],
        "hostapd.*": ["get_clients"],
        "luci-rpc": ["getHostHints"],
        "file": ["read"],
    }
}


class Status:
    """A ubus status code answer (`result: [code]`)."""

    def __init__(self, code: int) -> None:
        self.code = code


class RpcError:
    """A JSON-RPC error answer."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message


class Stall:
    """An answer that never comes."""


class FakeRouter:
    """Answers ubus JSON-RPC calls from `answers`, keyed by (object, method)."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], Any] = default_answers()
        self.acls = copy.deepcopy(DEFAULT_ACLS)
        self.password = PASSWORD
        self.session_valid = True
        self.calls: list[tuple[str, str, dict]] = []
        self.logins = 0
        self.port = 0
        self._stalled: list[asyncio.Future] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/ubus"

    def calls_of(self, obj: str, method: str) -> list[dict]:
        return [p for o, m, p in self.calls if (o, m) == (obj, method)]

    def release(self) -> None:
        for fut in self._stalled:
            if not fut.done():
                fut.set_result(None)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        session, obj, method, params = body["params"]
        self.calls.append((obj, method, params))
        if (obj, method) == ("session", "login"):
            self.logins += 1
            answer = self.answers.get(("session", "login"))
            if answer is None:
                if params.get("password") != self.password:
                    answer = Status(6)
                else:
                    self.session_valid = True
                    answer = {"ubus_rpc_session": SESSION, "acls": self.acls}
        elif session != SESSION or not self.session_valid:
            answer = RpcError(-32002, "Access denied")
        else:
            answer = self.answers.get((obj, method), RpcError(-32000, "Object not found"))
        if callable(answer) and not isinstance(answer, type):
            answer = answer(params)
        if isinstance(answer, Stall):
            fut = asyncio.get_running_loop().create_future()
            self._stalled.append(fut)
            await fut
            answer = Status(7)
        if isinstance(answer, bytes):
            return web.Response(body=answer)
        if isinstance(answer, RpcError):
            reply = {"jsonrpc": "2.0", "id": body["id"],
                     "error": {"code": answer.code, "message": answer.message}}
        elif isinstance(answer, Status):
            reply = {"jsonrpc": "2.0", "id": body["id"], "result": [answer.code]}
        else:
            reply = {"jsonrpc": "2.0", "id": body["id"], "result": [0, answer]}
        return web.json_response(reply)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    return


@pytest.fixture
async def router(socket_enabled, aiohttp_server):
    fake = FakeRouter()
    app = web.Application()
    app.router.add_post("/ubus", fake.handle)
    server = await aiohttp_server(app)
    fake.port = server.port
    yield fake
    fake.release()


@pytest.fixture
def make_entry(router: FakeRouter) -> Callable[..., MockConfigEntry]:
    def _make(**overrides) -> MockConfigEntry:
        data = {
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
        return MockConfigEntry(
            domain=DOMAIN, data=data, unique_id=data["address"],
            title=f"{data['id']} - {data['address']}",
        )

    return _make


@pytest.fixture
def short_ubus_timeout(monkeypatch) -> float:
    """Per-call timeout of 0.2 s instead of 15 s."""
    from custom_components.openwrt import ubus

    monkeypatch.setattr(ubus, "DEFAULT_TIMEOUT", 0.2)
    return 0.2
