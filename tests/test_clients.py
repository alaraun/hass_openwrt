"""Client trackers and client signal sensors across routers."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from .conftest import CLIENT_MAC, HOST_IP

TRACKER = "device_tracker.laptop"
SIGNAL = "sensor.laptop_wi_fi_signal"
TRACKER_UID = f"openwrt.client.{CLIENT_MAC}"
SIGNAL_UID = f"openwrt.client.{CLIENT_MAC}.signal"


def _enabled(hass: HomeAssistant, entry, mac: str = CLIENT_MAC, object_id: str = "laptop") -> None:
    """Register the client's tracker for `entry`, enabled (as a user would)."""
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "device_tracker", "openwrt", f"openwrt.client.{mac}",
        config_entry=entry, suggested_object_id=object_id,
    )


async def _setup(hass, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(hass, freezer, seconds: float = 31) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_tracker_is_disabled_and_has_no_device(hass, router, make_entry):
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, CLIENT_MAC)}
    )
    entry = make_entry()
    await _setup(hass, entry)
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id("device_tracker", "openwrt", TRACKER_UID)
    assert entity_id == TRACKER
    assert reg.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert reg.async_get(entity_id).device_id is None
    signal = reg.async_get(reg.async_get_entity_id("sensor", "openwrt", SIGNAL_UID))
    assert signal.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.identifiers for d in devices] == [{("id", "test-ap")}]


async def test_tracker_follows_the_client(hass, router, make_entry, freezer):
    entry = make_entry()
    _enabled(hass, entry)
    await _setup_loaded(hass, entry)
    state = hass.states.get(TRACKER)
    assert state.state == "home"
    assert state.attributes["ip"] == HOST_IP
    assert state.attributes["host_name"] == "laptop"
    assert state.attributes["mac"] == CLIENT_MAC
    assert state.attributes["router"] == "test-ap"
    assert state.attributes["ssid"] == "TestNet"

    # a signal change writes nothing
    reported = state.last_reported
    router.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {CLIENT_MAC: {"signal": -70}}}
    await _poll(hass, freezer)
    assert hass.states.get(TRACKER).last_reported == reported

    router.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {}}
    await _poll(hass, freezer)
    gone = hass.states.get(TRACKER)
    assert gone.state == "not_home"
    assert "router" not in gone.attributes

    # the registered tracker comes back after a reload, while the client is away
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(TRACKER).state == "not_home"


async def _two_routers(hass, router_factory, make_entry):
    first = await router_factory()
    second = await router_factory()
    second.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {}}
    entry_a = make_entry(first, id="ap-a", unique_id="a")
    _enabled(hass, entry_a)
    await _setup_loaded(hass, entry_a)
    await _setup(hass, make_entry(second, id="ap-b", unique_id="b"))
    assert hass.states.get(TRACKER).attributes["router"] == "ap-a"
    return first, second


def _record(hass, entity_id) -> list[str]:
    states: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda ev: states.append(ev.data["new_state"].state)
        if ev.data["entity_id"] == entity_id else None,
    )
    return states


async def _poll_only(hass, entry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_one_tracker_for_a_client_that_roams(hass, router_factory, make_entry):
    """A polls after the client moved to B, whose last list does not have it yet:
    B is asked at once, and the tracker never shows not_home."""
    first, second = await _two_routers(hass, router_factory, make_entry)
    entry_a = hass.config_entries.async_entries("openwrt")[0]
    states = _record(hass, TRACKER)
    first.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {}}
    second.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {CLIENT_MAC: {"signal": -60}}}
    polls_b = len(second.calls_of("system", "info"))
    await _poll_only(hass, entry_a)
    assert len(second.calls_of("system", "info")) == polls_b + 1

    state = hass.states.get(TRACKER)
    assert state.state == "home"
    assert state.attributes["router"] == "ap-b"
    assert "not_home" not in states
    trackers = [e for e in er.async_get(hass).entities.values() if e.domain == "device_tracker"]
    assert len(trackers) == 1
    assert trackers[0].config_entry_id == entry_a.entry_id


async def test_roaming_back_is_confirmed_again(hass, router_factory, make_entry):
    first, second = await _two_routers(hass, router_factory, make_entry)
    entry_a, entry_b = hass.config_entries.async_entries("openwrt")
    states = _record(hass, TRACKER)
    first.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {}}
    second.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {CLIENT_MAC: {"signal": -60}}}
    await _poll_only(hass, entry_a)
    assert hass.states.get(TRACKER).attributes["router"] == "ap-b"
    # and back to A, which B does not know yet
    first.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {CLIENT_MAC: {"signal": -55}}}
    second.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {}}
    polls_a = len(first.calls_of("system", "info"))
    await _poll_only(hass, entry_b)
    assert len(first.calls_of("system", "info")) == polls_a + 1
    assert hass.states.get(TRACKER).attributes["router"] == "ap-a"
    assert "not_home" not in states


async def test_client_on_two_routers_shows_the_stronger(hass, router_factory, make_entry):
    first, second = await _two_routers(hass, router_factory, make_entry)
    entry_b = hass.config_entries.async_entries("openwrt")[1]
    second.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {CLIENT_MAC: {"signal": -40}}}
    await _poll_only(hass, entry_b)
    assert hass.states.get(TRACKER).attributes["router"] == "ap-b"


async def test_owner_in_the_registry_keeps_the_tracker(hass, router_factory, make_entry):
    """After a restart the other router may load first; it must not take the client."""
    first, second = await _two_routers(hass, router_factory, make_entry)
    entry_a, entry_b = hass.config_entries.async_entries("openwrt")
    assert await hass.config_entries.async_unload(entry_a.entry_id)
    assert await hass.config_entries.async_unload(entry_b.entry_id)
    await hass.async_block_till_done()
    second.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {CLIENT_MAC: {"signal": -60}}}
    assert await hass.config_entries.async_setup(entry_b.entry_id)
    await hass.async_block_till_done()
    trackers = [e for e in er.async_get(hass).entities.values() if e.domain == "device_tracker"]
    assert [t.config_entry_id for t in trackers] == [entry_a.entry_id]


async def test_new_client_seen_by_two_routers_at_once(hass, router_factory, make_entry, caplog):
    import asyncio

    first, second = await _two_routers(hass, router_factory, make_entry)
    entry_a, entry_b = hass.config_entries.async_entries("openwrt")
    new_mac = "02:00:00:00:00:02"
    for fake in (first, second):
        fake.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {new_mac: {"signal": -60}}}
    await asyncio.gather(entry_a.runtime_data.async_refresh(), entry_b.runtime_data.async_refresh())
    await hass.async_block_till_done(wait_background_tasks=True)
    uids = [e.unique_id for e in er.async_get(hass).entities.values() if e.domain == "device_tracker"]
    assert sorted(uids) == sorted([TRACKER_UID, f"openwrt.client.{new_mac}"])
    assert "already exists" not in caplog.text


async def test_leaving_is_confirmed_by_the_other_routers(hass, router_factory, make_entry):
    first, second = await _two_routers(hass, router_factory, make_entry)
    entry_a = hass.config_entries.async_entries("openwrt")[0]
    states = _record(hass, TRACKER)
    first.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {}}
    polls_b = len(second.calls_of("system", "info"))
    await _poll_only(hass, entry_a)
    assert len(second.calls_of("system", "info")) == polls_b + 1
    assert states == ["not_home"]


async def test_leaving_with_one_router_needs_no_confirmation(hass, router, make_entry):
    entry = make_entry()
    _enabled(hass, entry)
    await _setup_loaded(hass, entry)
    router.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {}}
    calls = len(router.calls_of("system", "info"))
    await _poll_only(hass, entry)
    assert len(router.calls_of("system", "info")) == calls + 1
    assert hass.states.get(TRACKER).state == "not_home"


async def test_failed_router_keeps_its_clients(hass, router, make_entry, freezer):
    entry = make_entry()
    _enabled(hass, entry)
    await _setup_loaded(hass, entry)
    router.answers[("system", "info")] = b"not json"
    await _poll(hass, freezer)
    assert hass.states.get("binary_sensor.openwrt_test_ap").state == STATE_UNAVAILABLE
    assert hass.states.get(TRACKER).state == "home"


async def test_tracker_waits_for_home_assistant_to_start(hass, router, make_entry):
    entry = make_entry()
    _enabled(hass, entry)
    hass.set_state(CoreState.starting)
    await _setup_loaded(hass, entry)
    assert hass.states.get(TRACKER).state == STATE_UNAVAILABLE
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert hass.states.get(TRACKER).state == "home"


async def test_router_device_cannot_be_deleted_client_devices_can(hass, router, make_entry):
    from custom_components.openwrt import async_remove_config_entry_device

    entry = make_entry()
    await _setup(hass, entry)
    dev_reg = dr.async_get(hass)
    router_device = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)[0]
    client_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, CLIENT_MAC)}
    )
    assert not await async_remove_config_entry_device(hass, entry, router_device)
    assert await async_remove_config_entry_device(hass, entry, client_device)


async def test_signal_sensor_when_enabled(hass, router, make_entry, freezer):
    reg = er.async_get(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    reg.async_get_or_create(
        "sensor", "openwrt", SIGNAL_UID, config_entry=entry, suggested_object_id="laptop_wi_fi_signal"
    )
    await _setup_loaded(hass, entry)
    assert hass.states.get(SIGNAL).state == "-50"
    assert hass.states.get(SIGNAL).attributes["unit_of_measurement"] == "dBm"
    router.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {CLIENT_MAC: {"signal": -61}}}
    await _poll(hass, freezer)
    assert hass.states.get(SIGNAL).state == "-61"
    router.answers[("hostapd.phy0-ap0", "get_clients")] = {"clients": {}}
    await _poll(hass, freezer)
    assert hass.states.get(SIGNAL).state == STATE_UNAVAILABLE


async def _setup_loaded(hass, entry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_claim_before_the_registry_has_the_entity(hass):
    """Two routers that see a new client in one loop iteration: only the first owns it."""
    from custom_components.openwrt.clients import claim, release_claims

    first, second = MockConfigEntry(domain="openwrt"), MockConfigEntry(domain="openwrt")
    uid = "openwrt.client.02:00:00:00:00:03"
    assert claim(hass, first, "device_tracker", "02:00:00:00:00:03", uid)
    assert not claim(hass, second, "device_tracker", "02:00:00:00:00:03", uid)
    assert claim(hass, first, "device_tracker", "02:00:00:00:00:03", uid)
    release_claims(hass, first)
    assert claim(hass, second, "device_tracker", "02:00:00:00:00:03", uid)
