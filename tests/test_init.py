"""Setup, polling and error handling of a config entry against the fake router."""

from __future__ import annotations

import logging
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import MATCH_ALL, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import UCI_WIRELESS, Stall, Status

CLIENTS = "sensor.openwrt_test_ap_wireless_testnet_clients"
UPTIME = "sensor.openwrt_test_ap_system_uptime"
MEMORY = "sensor.openwrt_test_ap_system_memory_free"
HOSTS = "sensor.openwrt_test_ap_known_hosts"
ONLINE = "binary_sensor.openwrt_test_ap"


async def _setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float = 31) -> None:
    """Advance past the next poll (the interval is 30 s, plus up to 1 s of jitter)."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    # interval refreshes run as background tasks
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_creates_entities_with_stable_ids(hass, router, make_entry):
    entry = make_entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get(CLIENTS).state == "1"
    assert hass.states.get(HOSTS).state == "1"
    assert hass.states.get(ONLINE).state == "on"
    assert hass.states.get(CLIENTS).attributes["friendly_name"] == (
        "OpenWrt [test-ap] Wireless [TestNet] clients"
    )
    assert hass.states.get(ONLINE).attributes["friendly_name"] == "OpenWrt [test-ap]"

    ent_reg = er.async_get(hass)
    assert ent_reg.async_get(CLIENTS).unique_id == "sensor.openwrt.test-ap.phy0-ap0.clients"
    assert ent_reg.async_get(UPTIME).unique_id == "sensor.openwrt.test-ap.system_uptime"
    assert ent_reg.async_get(ONLINE).unique_id == "sensor.openwrt.test-ap"

    device = dr.async_get(hass).async_get_device_by_identifier(("id", "test-ap"), entry.entry_id)
    assert device.model == "Test Router"
    assert device.sw_version == "25.12.0 r1"


async def test_volatile_attributes_are_not_recorded(hass, router, make_entry):
    await _setup(hass, make_entry())
    for entity_id in (CLIENTS, MEMORY, HOSTS, "sensor.openwrt_test_ap_system_load"):
        state = hass.states.get(entity_id)
        assert MATCH_ALL in state.state_info["unrecorded_attributes"], entity_id
    attrs = hass.states.get(CLIENTS).attributes
    assert attrs["02:00:00:00:00:01".upper()] == "-50 dBm | IP: 192.0.2.10 | Name: laptop"


async def test_modem_mode_has_no_collection_time_attribute(hass, router, make_entry):
    router.answers[("file", "read")] = {
        "data": '{"mode": "5G NSA", "operator": "Test", "timestamp": 1700000000, "lte_rsrp": -90}'
    }
    await _setup(hass, make_entry())
    state = hass.states.get("sensor.openwrt_test_ap_modem_mode")
    assert state.state == "5G NSA"
    assert state.attributes["operator"] == "Test"
    assert "last_update" not in state.attributes
    assert hass.states.get("sensor.openwrt_test_ap_modem_last_update").state == (
        "2023-11-14T22:13:20+00:00"
    )


async def test_uptime_is_a_boot_time_that_changes_only_on_reboot(
    hass, router, make_entry, freezer
):
    freezer.move_to("2026-01-01T12:00:00+00:00")
    await _setup(hass, make_entry())
    boot = hass.states.get(UPTIME)
    assert boot.state == "2026-01-01T11:43:20+00:00"  # 1000 s before now

    # 31 s later the device counts 30 s more: latency and rounding, same boot
    router.answers[("system", "info")]["uptime"] = 1030
    await _poll(hass, freezer, 31)
    assert hass.states.get(UPTIME).last_changed == boot.last_changed
    assert hass.states.get(UPTIME).last_reported == boot.last_reported

    # rebooted: the uptime went down
    router.answers[("system", "info")]["uptime"] = 20
    await _poll(hass, freezer, 31)
    assert hass.states.get(UPTIME).state == "2026-01-01T12:00:42+00:00"


async def test_uptime_follows_a_clock_jump_beyond_the_tolerance(
    hass, router, make_entry, freezer
):
    freezer.move_to("2026-01-01T12:00:00+00:00")
    await _setup(hass, make_entry())
    router.answers[("system", "info")]["uptime"] = 1030
    await _poll(hass, freezer, 30 + 60)
    # exactly the tolerance: kept
    assert hass.states.get(UPTIME).state == "2026-01-01T11:43:20+00:00"
    # 61 s off: follows
    router.answers[("system", "info")]["uptime"] = 1060
    await _poll(hass, freezer, 31)
    assert hass.states.get(UPTIME).state == "2026-01-01T11:44:21+00:00"


async def test_uptime_reboot_within_the_tolerance(hass, router, make_entry, freezer):
    """A second reboot right after a boot moves the boot time by less than the tolerance."""
    freezer.move_to("2026-01-01T12:00:00+00:00")
    router.answers[("system", "info")]["uptime"] = 20
    await _setup(hass, make_entry())
    assert hass.states.get(UPTIME).state == "2026-01-01T11:59:40+00:00"
    router.answers[("system", "info")]["uptime"] = 5
    await _poll(hass, freezer, 31)
    assert hass.states.get(UPTIME).state == "2026-01-01T12:00:26+00:00"


async def test_system_info_timeout_fails_the_poll_with_a_message(
    hass, router, make_entry, short_ubus_timeout, caplog
):
    """A timed-out call marks the device unavailable; no zeros, no empty warning."""
    entry = make_entry()
    await _setup(hass, entry)
    info = router.answers[("system", "info")]
    router.answers[("system", "info")] = Stall()
    # a real clock: the client's timeout must run out
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    for entity_id in (ONLINE, MEMORY, UPTIME, CLIENTS):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id
    assert "test-ap: system.info: no answer within 0.2 s" in caplog.text
    assert "failed to get system info: \n" not in caplog.text
    assert not [
        r for r in caplog.records
        if r.name.startswith("custom_components.openwrt") and r.levelno >= logging.WARNING
        and not r.getMessage().strip().split(":")[-1].strip()
    ]

    router.release()
    router.answers[("system", "info")] = info
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ONLINE).state == "on"
    assert hass.states.get(MEMORY).state == "256.0"


async def test_polls_wait_until_home_assistant_has_started(hass, router, make_entry, freezer):
    from homeassistant.core import CoreState

    hass.set_state(CoreState.starting)
    await _setup(hass, make_entry())  # the first refresh runs while starting
    assert hass.states.get(CLIENTS).state == "1"
    calls = len(router.calls)
    await _poll(hass, freezer)
    assert len(router.calls) == calls
    assert hass.states.get(ONLINE).state == "on"
    hass.set_state(CoreState.running)
    await _poll(hass, freezer)
    assert len(router.calls) > calls


async def test_system_info_rpc_error_leaves_other_sensors(hass, router, make_entry, freezer):
    await _setup(hass, make_entry())
    router.answers[("system", "info")] = Status(2)
    await _poll(hass, freezer)
    assert hass.states.get(MEMORY).state == STATE_UNAVAILABLE
    assert hass.states.get(UPTIME).state == STATE_UNAVAILABLE
    assert hass.states.get(CLIENTS).state == "1"
    assert hass.states.get(ONLINE).state == "on"


async def test_denied_optional_call_does_not_start_reauth(hass, router, make_entry, freezer):
    from .conftest import RpcError

    entry = make_entry()
    await _setup(hass, entry)
    router.answers[("hostapd.phy0-ap0", "get_clients")] = RpcError(-32002, "Access denied")
    await _poll(hass, freezer)
    assert hass.states.get(CLIENTS).state == STATE_UNAVAILABLE
    assert hass.states.get(ONLINE).state == "on"
    assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_rejected_password_starts_reauth(hass, router, make_entry):
    router.password = "changed"
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_unreachable_router_retries_setup(hass, router, make_entry, short_ubus_timeout):
    router.answers[("system", "info")] = Stall()
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    router.release()


async def test_new_interface_gets_a_sensor_on_the_next_poll(hass, router, make_entry, freezer):
    await _setup(hass, make_entry())
    values = dict(UCI_WIRELESS["values"])
    values["guest"] = {
        ".type": "wifi-iface", ".name": "guest", ".index": 2,
        "device": "radio0", "mode": "ap", "ssid": "Guest",
    }
    router.answers[("uci", "get")] = {"values": values}
    router.answers[("hostapd.phy0-ap1", "get_clients")] = {"clients": {}}
    await _poll(hass, freezer)
    assert hass.states.get("sensor.openwrt_test_ap_wireless_guest_clients").state == "0"
    assert hass.states.get("sensor.openwrt_test_ap_wireless_total_clients").state == "1"


async def test_unload_and_reload(hass, router, make_entry, freezer):
    entry = make_entry()
    await _setup(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    calls = len(router.calls)
    device = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _poll(hass, freezer)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert len(router.calls) == calls
    assert not device._listeners


async def test_missing_hostapd_object_makes_the_sensor_unavailable(
    hass, router, make_entry, freezer, caplog
):
    await _setup(hass, make_entry())
    del router.answers[("hostapd.phy0-ap0", "get_clients")]
    await _poll(hass, freezer)
    assert hass.states.get(CLIENTS).state == STATE_UNAVAILABLE
    assert hass.states.get(ONLINE).state == "on"
    assert "'clients' key not found" not in caplog.text


async def test_wireless_status_failure_falls_back_to_uci_once(hass, router, make_entry, freezer):
    await _setup(hass, make_entry())
    await _poll(hass, freezer)
    assert len(router.calls_of("network.wireless", "status")) == 1
    assert len(router.calls_of("uci", "get")) == 2


async def test_diagnostics_redact_credentials(hass, router, make_entry):
    from custom_components.openwrt.diagnostics import async_get_config_entry_diagnostics

    entry = make_entry()
    await _setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["password"] == "**REDACTED**"
    assert diag["entry"]["username"] == "**REDACTED**"
    assert diag["wireless_via_uci"] is True
    assert diag["data"]["wireless"]["phy0-ap0"]["clients"] == 1
