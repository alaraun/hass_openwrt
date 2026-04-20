import asyncio
import itertools
import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import logging

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT: int = 15


class UbusError(Exception):
    """Base class for Ubus errors."""


class UbusConnectionError(UbusError):
    """Error connecting to Ubus."""


class UbusAuthError(UbusError):
    """Authentication error."""


class UbusPermissionError(UbusError):
    """Insufficient permissions."""


class UbusObjectNotFoundError(UbusError):
    """Ubus object or method not found."""


class UbusRPCError(UbusError):
    """Generic RPC error."""


class Ubus:
    def __init__(
        self,
        hass,
        url: str,
        username: str,
        password: str,
        timeout: int = DEFAULT_TIMEOUT,
        verify: bool = True,
    ):
        self._hass = hass
        self.url = url
        self.username = username
        self.password = password
        self.timeout = timeout
        self.verify = verify
        self.session_id = ""
        self._rpc_counter = itertools.count(1)
        self.acls = {}
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
            async with self._login_lock:
                if not self.session_id:
                    await self._login()
            return await self._api_call(rpc_method, subsystem, method, params)
        except UbusObjectNotFoundError as err:
            _LOGGER.debug("api_call: object not found after login, returning empty: %s", err)
            return {}

    async def login(self):
        """Acquire the login lock and perform login. Safe to call concurrently."""
        async with self._login_lock:
            if not self.session_id:
                await self._login()

    async def _login(self):
        _LOGGER.debug("Logging in to Ubus...")
        try:
            result = await self._api_call(
                "call",
                "session",
                "login",
                dict(username=self.username, password=self.password),
                "00000000000000000000000000000000",
            )
        except UbusRPCError as err:
            raise UbusAuthError(f"Login failed: {err}") from err
        _LOGGER.debug("Login successful")
        self.session_id = result["ubus_rpc_session"]
        self.acls = result.get("acls", {})
        _LOGGER.debug("ACLs: %s", self.acls)

    async def _api_call(
        self,
        rpc_method: str,
        subsystem: str,
        method: str,
        params: dict,
        session: str = None,
    ) -> dict:
        _params = [session if session else self.session_id, subsystem]
        if method:
            _params.append(method)
        _params.append(params if params else {})

        payload = {
            "jsonrpc": "2.0",
            "id": next(self._rpc_counter),
            "method": rpc_method,
            "params": _params,
        }
        _LOGGER.debug("API call payload: %s", payload)

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with self._session.post(
                self.url, json=payload, timeout=timeout
            ) as response:
                if response.status != 200:
                    _LOGGER.error("api_call http error: %s", response.status)
                    raise UbusConnectionError(f"HTTP error {response.status} from {self.url}")
                # content_type=None avoids strict checking; rpcd may omit application/json
                json_response = await response.json(content_type=None)
        except (aiohttp.ClientError, RuntimeError) as err:
            _LOGGER.debug("api_call exception: %s", err)
            raise UbusConnectionError(f"Communication error: {err}") from err

        _LOGGER.debug("Raw JSON response: %s", json_response)

        if "error" in json_response:
            code = json_response["error"].get("code")
            message = json_response["error"].get("message")
            if code == -32000:
                # Object not found — expected for optional ubus objects (mwan3, etc.)
                _LOGGER.debug("api_call: ubus object not found: %s", message)
                raise UbusObjectNotFoundError(message)
            if code == -32002:
                _LOGGER.debug("api_call RPC error: %s", json_response["error"])
                raise UbusPermissionError(message)
            _LOGGER.error("api_call RPC error: %s", json_response["error"])
            raise UbusRPCError(f"RPC error: {message}")

        result = json_response["result"]
        if rpc_method == "list":
            return result
        result_code = result[0]
        if result_code == 8:
            raise UbusPermissionError("RPC error: not allowed")
        if result_code == 6:
            raise UbusPermissionError("RPC error: insufficient permissions")
        if result_code == 0:
            return json_response["result"][1] if len(result) > 1 else {}
        raise UbusRPCError(f"RPC error code: {result[0]}")
