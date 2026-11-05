"""JSON-RPC client for the OpenWrt ubus HTTP endpoint (uhttpd-mod-ubus)."""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT: float = 15
ANONYMOUS_SESSION = "00000000000000000000000000000000"


class UbusError(Exception):
    """Base class for Ubus errors."""


class UbusConnectionError(UbusError):
    """The endpoint did not answer, timed out, or sent a malformed reply."""


class UbusAuthError(UbusError):
    """The login was rejected."""


class UbusRPCError(UbusError):
    """A call was answered with an error."""


class UbusPermissionError(UbusRPCError):
    """The session may not call this object or method (ACL)."""


class UbusObjectNotFoundError(UbusRPCError):
    """The ubus object or method does not exist."""


class Ubus:
    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        username: str,
        password: str,
        timeout: float | None = None,
        verify: bool = True,
    ) -> None:
        self._hass = hass
        self.url = url
        self.username = username
        self.password = password
        self.timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        self.verify = verify
        self.session_id = ""
        self._rpc_counter = itertools.count(1)
        self.acls: dict[str, Any] = {}
        self._login_lock = asyncio.Lock()

    @property
    def _session(self) -> aiohttp.ClientSession:
        return async_get_clientsession(self._hass, verify_ssl=self.verify)

    async def api_call(
        self,
        subsystem: str,
        method: str,
        params: dict,
        rpc_method: str = "call",
    ) -> dict:
        """Call `subsystem.method`; log in first, and once more on a stale session.

        A missing object or method returns `{}`. Raises UbusConnectionError,
        UbusAuthError, or UbusRPCError (UbusPermissionError after a fresh login).
        """
        _LOGGER.debug("api_call: %s.%s params=%s", subsystem, method, params)
        try:
            if self.session_id:
                return await self._api_call(rpc_method, subsystem, method, params)
        except UbusPermissionError as err:
            _LOGGER.debug("Session stale, will re-login: %s", err)
            self.session_id = ""
        except UbusObjectNotFoundError as err:
            _LOGGER.debug("api_call: object not found, returning empty: %s", err)
            return {}

        try:
            await self.login()
            return await self._api_call(rpc_method, subsystem, method, params)
        except UbusObjectNotFoundError as err:
            _LOGGER.debug("api_call: object not found after login, returning empty: %s", err)
            return {}

    async def login(self) -> None:
        """Log in unless a session exists. Safe to call concurrently."""
        async with self._login_lock:
            if not self.session_id:
                await self._login()

    async def _login(self) -> None:
        _LOGGER.debug("Logging in to %s as %s", self.url, self.username)
        try:
            result = await self._api_call(
                "call",
                "session",
                "login",
                dict(username=self.username, password=self.password),
                ANONYMOUS_SESSION,
            )
        except UbusPermissionError as err:
            raise UbusAuthError(f"Login failed: {err}") from err
        except UbusRPCError as err:
            # rpcd not ready (the session object is missing while the device boots)
            raise UbusConnectionError(f"Login not answered: {err}") from err
        if not isinstance(result, dict) or "ubus_rpc_session" not in result:
            raise UbusConnectionError("Login not answered: no session in the reply")
        self.session_id = result["ubus_rpc_session"]
        self.acls = result.get("acls", {})
        _LOGGER.debug("Login successful; ACLs: %s", self.acls)

    async def _api_call(
        self,
        rpc_method: str,
        subsystem: str,
        method: str,
        params: dict,
        session: str | None = None,
    ) -> Any:
        _params: list[Any] = [session if session else self.session_id, subsystem]
        if method:
            _params.append(method)
        _params.append(params if params else {})

        payload = {
            "jsonrpc": "2.0",
            "id": next(self._rpc_counter),
            "method": rpc_method,
            "params": _params,
        }
        # The session id and login parameters stay out of the log.
        _LOGGER.debug(
            "API call #%s: %s %s.%s", payload["id"], rpc_method, subsystem, method
        )
        call = f"{subsystem}.{method}" if method else subsystem

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with self._session.post(
                self.url, json=payload, timeout=timeout
            ) as response:
                if response.status != 200:
                    raise UbusConnectionError(
                        f"{call}: HTTP {response.status} from {self.url}"
                    )
                # content_type=None: rpcd may omit application/json
                json_response = await response.json(content_type=None)
        except TimeoutError as err:
            # aiohttp's total timeout raises a bare TimeoutError (empty message)
            raise UbusConnectionError(
                f"{call}: no answer within {self.timeout:g} s"
            ) from err
        except (aiohttp.ClientError, RuntimeError) as err:
            raise UbusConnectionError(f"{call}: {type(err).__name__}: {err}") from err
        except ValueError as err:
            raise UbusConnectionError(f"{call}: malformed reply: {err}") from err

        if not isinstance(json_response, dict):
            raise UbusConnectionError(f"{call}: malformed reply")

        if "error" in json_response:
            code = json_response["error"].get("code")
            message = json_response["error"].get("message")
            if code == -32000:
                # Object not found: expected for optional objects (mwan3, luci-rpc)
                _LOGGER.debug("%s: ubus object not found: %s", call, message)
                raise UbusObjectNotFoundError(f"{call}: {message}")
            if code == -32002:
                _LOGGER.debug("%s: access denied: %s", call, message)
                raise UbusPermissionError(f"{call}: {message}")
            raise UbusRPCError(f"{call}: RPC error {code}: {message}")

        result = json_response.get("result")
        if rpc_method == "list":
            return result
        if not isinstance(result, list) or not result:
            raise UbusConnectionError(f"{call}: malformed reply")
        result_code = result[0]
        if result_code == 0:
            _LOGGER.debug("API call #%s answered", payload["id"])
            return result[1] if len(result) > 1 else {}
        if result_code in (6, 8):
            # 6 UBUS_STATUS_PERMISSION_DENIED, 8 UBUS_STATUS_NOT_SUPPORTED
            raise UbusPermissionError(f"{call}: not allowed (ubus status {result_code})")
        raise UbusRPCError(f"{call}: ubus status {result_code}")
