"""Home Assistant side of a STREAM pack: its config entry and its members"""

import logging
from collections.abc import Callable

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_PACK_ID, CONF_PACK_SERIAL, DOMAIN
from .eflib import DeviceBase
from .eflib.devices import stream_ac_5000
from .eflib.pack import PACK_KEYS, Pack

PACK_PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.NUMBER, Platform.SWITCH]

_PACKS_KEY = f"{DOMAIN}_packs"
# Every unit used to own this control, so its oldest entity tells which one came first
_FIRST_UNIT_KEY = "energy_backup_battery_level"

_LOGGER = logging.getLogger(__name__)


def is_pack_entry(entry: ConfigEntry) -> bool:
    return CONF_PACK_ID in entry.data


def _pack(hass: HomeAssistant, pack_id: str) -> Pack:
    packs: dict[str, Pack] = hass.data.setdefault(_PACKS_KEY, {})
    return packs.setdefault(pack_id, Pack(pack_id))


def _first_unit_serial(hass: HomeAssistant, fallback: str) -> str:
    # The pack takes over the entities of the first unit added, so that its history and
    # the dashboards built on it carry on. Which unit connects first varies, so read it
    # from the registry instead, leaving out the units other packs have already claimed
    claimed = {
        entry.data[CONF_PACK_SERIAL]
        for entry in hass.config_entries.async_entries(DOMAIN)
        if is_pack_entry(entry)
    }
    candidates = [
        entity
        for entity in er.async_get(hass).entities.values()
        if entity.platform == DOMAIN
        and entity.unique_id.endswith(f"_{_FIRST_UNIT_KEY}")
        and _unit_serial(entity.unique_id) not in claimed
    ]
    if not candidates:
        return fallback
    return _unit_serial(min(candidates, key=lambda e: e.created_at).unique_id)


def _unit_serial(unique_id: str) -> str:
    return unique_id.removeprefix("ef_").partition("_")[0]


async def _async_ensure_entry(hass: HomeAssistant, pack_id: str, serial: str) -> None:
    unique_id = f"pack_{pack_id}"
    if any(
        entry.unique_id == unique_id
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        return

    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={
            CONF_PACK_ID: pack_id,
            CONF_PACK_SERIAL: _first_unit_serial(hass, serial),
        },
    )


def async_track_member(hass: HomeAssistant, device: DeviceBase) -> Callable[[], None]:
    """Join a STREAM unit to its pack once it reports one, and return how to leave it"""
    if not isinstance(device, stream_ac_5000.Device):
        return lambda: None

    joined: list[Pack] = []

    @callback
    def join(pack_id: str | None) -> None:
        if pack_id is None or joined:
            return
        pack = _pack(hass, pack_id)
        pack.attach(device)
        joined.append(pack)
        _async_remove_unit_duplicates(hass, pack)
        hass.async_create_task(_async_ensure_entry(hass, pack_id, device.serial_number))

    device.register_state_update_callback(join, "pack_id")
    join(device.pack_id)

    @callback
    def leave() -> None:
        device.remove_state_update_callback(join, "pack_id")
        for pack in joined:
            pack.detach(device)

    return leave


def _async_remove_unit_duplicates(hass: HomeAssistant, pack: Pack) -> None:
    """Remove what the pack's units used to own, once the pack has its own identity"""
    if not pack.serial_number:
        return

    registry = er.async_get(hass)
    units = {member.serial_number for member in pack.members} - {pack.serial_number}
    for entity in list(registry.entities.values()):
        if entity.platform != DOMAIN or _unit_serial(entity.unique_id) not in units:
            continue
        if (
            entity.unique_id.partition(f"{_unit_serial(entity.unique_id)}_")[2]
            in PACK_KEYS
        ):
            _LOGGER.debug("Removing %s, now owned by the pack", entity.entity_id)
            registry.async_remove(entity.entity_id)


async def async_setup_pack_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    pack = _pack(hass, entry.data[CONF_PACK_ID])
    pack.serial_number = entry.data[CONF_PACK_SERIAL]
    entry.runtime_data = pack
    await hass.config_entries.async_forward_entry_setups(entry, PACK_PLATFORMS)
    _async_remove_unit_duplicates(hass, pack)
    return True
