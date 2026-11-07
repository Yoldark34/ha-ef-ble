"""A pack of linked STREAM units, seen as one device"""

import logging
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from bleak.exc import BleakError

from .entity import controls
from .entity.base import dynamic
from .exceptions import NotConnectedError
from .props import Field, computed_field
from .props.enums import IntFieldValue
from .props.transforms import pnegative, ppositive
from .props.updatable_props import UpdatableProps

if TYPE_CHECKING:
    from .devices.stream_ac_5000 import Device as Member

_LOGGER = logging.getLogger(__name__)

# Which member field feeds which field of the pack. Every unit posts the same totals, so
# the latest post wins and the pack keeps reporting while any one unit is connected
_MEMBER_FIELDS = {
    "pack_battery_level": "battery_level",
    "pack_battery_power": "battery_power",
    "pack_grid_power": "grid_power",
    "pack_house_grid_power": "house_grid_power",
    "pack_load_system": "load_system",
    "pack_load_from_battery": "load_from_battery",
    "pack_load_from_grid": "load_from_grid",
    "pack_battery_charge_limit_max": "battery_charge_limit_max",
    "pack_battery_charge_limit_min": "battery_charge_limit_min",
    "pack_energy_backup": "energy_backup",
    "pack_energy_backup_battery_level": "energy_backup_battery_level",
    "pack_unit_count": "system_unit_count",
    "pack_feed_grid_pow_limit": "feed_grid_pow_limit",
    "pack_feed_grid_pow_max": "feed_grid_pow_max",
    "pack_grid_in_power_limit": "grid_in_power_limit",
    "pack_report_interval": "report_interval",
}

# What the app reports as the pack's charging limit, with no higher value on offer
_GRID_IN_POWER_MAX = 3000

# Keys the pack owns, so a member must not keep an entity of its own for them
PACK_KEYS = frozenset(
    {
        *_MEMBER_FIELDS.values(),
        "battery_ac_input_power",
        "battery_ac_output_power",
    }
)


class PackStatus(IntFieldValue):
    UNKNOWN = -1

    OFFLINE = 0
    WAITING = 1
    DEGRADED = 2
    READY = 3


class Pack(UpdatableProps):
    """STREAM 5000 Pack"""

    battery_level = Field[int]()
    battery_power = Field[int]()
    grid_power = Field[int]()
    house_grid_power = Field[int]()
    load_system = Field[int]()
    load_from_battery = Field[int]()
    load_from_grid = Field[int]()
    battery_charge_limit_max = Field[int]()
    battery_charge_limit_min = Field[int]()
    energy_backup = Field[bool]()
    energy_backup_battery_level = Field[int]()
    system_unit_count = Field[int]()
    feed_grid_pow_limit = Field[int]()
    feed_grid_pow_max = Field[int]()
    grid_in_power_limit = Field[int]()
    report_interval = Field[float]()

    def __init__(self, pack_id: str, serial_number: str = "") -> None:
        super().__init__()
        self._pack_id = pack_id
        self.serial_number = serial_number
        self._members: dict[Member, list[tuple[Callable[[Any], None], str]]] = {}
        self._callbacks_map: dict[str, set[Callable[[], None]]] = defaultdict(set)
        self._state_callbacks: dict[str, set[Callable[[Any], None]]] = defaultdict(set)

    @property
    def address(self) -> str:
        return f"pack_{self._pack_id}"

    @property
    def name(self) -> str:
        return "Pack Stream 5000"

    @property
    def device(self) -> str:
        return self.__doc__ or ""

    @property
    def members(self) -> "list[Member]":
        return list(self._members)

    @property
    def is_connected(self) -> bool:
        return any(member.is_connected for member in self._members)

    @computed_field
    def pack_status(self) -> PackStatus:
        """What an automation can expect of the pack right now"""
        connected = sum(member.is_connected for member in self._members)
        if not connected:
            return PackStatus.OFFLINE
        # Nothing is known of the pack until a unit's first full report, a few minutes
        # after it connects, and the controls need those values to know their limits
        if self.energy_backup_battery_level is None:
            return PackStatus.WAITING
        if connected < (self.system_unit_count or 0):
            return PackStatus.DEGRADED
        return PackStatus.READY

    @computed_field
    def battery_ac_input_power(self) -> float | None:
        return ppositive()(self.battery_power)

    @computed_field
    def battery_ac_output_power(self) -> float | None:
        return pnegative()(self.battery_power)

    def attach(self, member: "Member") -> None:
        if member in self._members:
            return

        self._members[member] = []
        for member_field, pack_field in _MEMBER_FIELDS.items():

            def update(value: Any, pack_field: str = pack_field) -> None:
                self._update(pack_field, value)

            member.register_state_update_callback(update, member_field)
            self._members[member].append((update, member_field))
            update(getattr(member, member_field))
        self._publish_availability()

    def detach(self, member: "Member") -> None:
        for update, member_field in self._members.pop(member, []):
            member.remove_state_update_callback(update, member_field)
        self._publish_availability()

    def _publish_availability(self) -> None:
        # Entities read `is_connected` when they write their state, and nothing else
        # makes them write it when the last unit leaves
        self.reset_updated()
        self._notify_updated()
        for propname in list(self._state_callbacks):
            self.update_state(propname, getattr(self, propname))
        for propname in list(self._callbacks_map):
            self.update_callback(propname)

    def _update(self, pack_field: str, value: Any) -> None:
        if value is None:
            return
        self.reset_updated()
        setattr(self, pack_field, value)
        self._notify_updated()

    def register_callback(
        self, callback: Callable[[], None], propname: str | None = None
    ) -> None:
        self._callbacks_map[propname or ""].add(callback)

    def remove_callback(
        self, callback: Callable[[], None], propname: str | None = None
    ) -> None:
        self._callbacks_map[propname or ""].discard(callback)

    def register_state_update_callback(
        self, state_update_callback: Callable[[Any], None], propname: str
    ) -> None:
        self._state_callbacks[propname].add(state_update_callback)

    def remove_state_update_callback(
        self, callback: Callable[[Any], None], propname: str
    ) -> None:
        self._state_callbacks[propname].discard(callback)

    def update_callback(self, propname: str) -> None:
        for callback in self._callbacks_map[propname]:
            callback()

    def update_state(self, propname: str, value: Any) -> None:
        for callback in self._state_callbacks[propname]:
            callback(value)

    def _by_priority(self) -> "list[Member]":
        # Roles change over the life of a pack, so read them at each command. The sort
        # is stable: units with no known role keep the order they joined in
        return sorted(
            (member for member in self._members if member.is_connected),
            key=lambda member: not member.is_master,
        )

    async def _command(self, name: str, *args: Any) -> None:
        """Send to the master, falling back on the other units if it can't be reached"""
        last_error: Exception | None = None
        for member in self._by_priority():
            try:
                await getattr(member, name)(*args)
            except (NotConnectedError, BleakError, TimeoutError) as e:
                _LOGGER.debug("%s failed on %s: %s", name, member.name, e)
                last_error = e
            else:
                return

        raise NotConnectedError("No unit of the pack can be reached") from last_error

    @controls.battery(battery_charge_limit_max, min=dynamic(battery_charge_limit_min))
    async def set_battery_charge_limit_max(self, limit: float):
        await self._command("set_battery_charge_limit_max", limit)
        return True

    @controls.battery(battery_charge_limit_min, max=dynamic(battery_charge_limit_max))
    async def set_battery_charge_limit_min(self, limit: float):
        await self._command("set_battery_charge_limit_min", limit)
        return True

    @controls.battery(
        energy_backup_battery_level,
        min=dynamic(battery_charge_limit_min),
        max=dynamic(battery_charge_limit_max),
    )
    async def set_energy_backup_battery_level(self, value: float):
        await self._command("set_energy_backup_battery_level", value)
        return True

    @controls.power(feed_grid_pow_limit, max=dynamic(feed_grid_pow_max))
    async def set_feed_grid_pow_limit(self, value: float):
        await self._command("set_feed_grid_pow_limit", value)
        return True

    @controls.power(grid_in_power_limit, max=_GRID_IN_POWER_MAX)
    async def set_grid_in_power_limit(self, value: float):
        await self._command("set_grid_in_power_limit", value)
        return True

    @controls.duration(report_interval, min=10, max=120, step=5)
    async def set_report_interval(self, seconds: float):
        # Each unit keeps its own, so unlike the pack's settings this goes to all of them
        members = [member for member in self._members if member.is_connected]
        if not members:
            raise NotConnectedError("No unit of the pack can be reached")
        for member in members:
            await member.set_report_interval(seconds)
        return True

    @controls.switch(energy_backup)
    async def enable_energy_backup(self, enabled: bool):
        await self._command("enable_energy_backup", enabled)
