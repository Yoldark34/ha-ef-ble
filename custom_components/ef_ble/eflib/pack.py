"""A pack of linked STREAM units, seen as one device"""

import asyncio
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
    from .devices.stream_ac_5000 import UnitReport

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
DEFAULT_REPORT_INTERVAL = 30

# Which member field feeds which field of its unit, for what only the unit's link carries
_MEMBER_UNIT_FIELDS = {
    "own_remaining_time_charging": "remaining_time_charging",
    "own_remaining_time_discharging": "remaining_time_discharging",
    "own_battery_full_capacity": "battery_full_capacity",
    "own_battery_remaining_energy": "battery_remaining_energy",
    "own_ac_ports": "ac_ports",
    "own_cell_temperature": "cell_temperature",
    "own_system_role": "system_role",
}

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


class _VirtualDevice(UpdatableProps):
    """What entities need of a device, for one that is not a connection of its own"""

    def __init__(self) -> None:
        super().__init__()
        self._callbacks_map: dict[str, set[Callable[[], None]]] = defaultdict(set)
        self._state_callbacks: dict[str, set[Callable[[Any], None]]] = defaultdict(set)

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

    def publish_availability(self) -> None:
        # Entities read `is_connected` when they write their state, and nothing else
        # makes them write it when a connection comes or goes
        self.reset_updated()
        self._notify_updated()
        for propname in list(self._state_callbacks):
            self.update_state(propname, getattr(self, propname))
        for propname in list(self._callbacks_map):
            self.update_callback(propname)


class PackUnit(_VirtualDevice):
    """
    One battery as the pack sees it

    Its own posts and the master's list of units feed the same fields, so the latest
    report wins and the unit keeps reporting while either of the two is reachable. Only
    what the master does not relay, temperature and capacity, needs the unit's own link.
    """

    battery_level_main = Field[int]()
    unit_battery_power = Field[int]()
    unit_grid_power = Field[int]()
    backup_port_power = Field[int]()
    remaining_time_charging = Field[int]()
    remaining_time_discharging = Field[int]()
    battery_full_capacity = Field[int]()
    battery_remaining_energy = Field[int]()
    ac_ports = Field[bool]()
    cell_temperature = Field[int]()
    system_role = Field[Any]()

    def __init__(self, pack: "Pack", serial_number: str, address: str) -> None:
        super().__init__()
        self._pack = pack
        self.serial_number = serial_number
        self.address = address

    @property
    def pack_address(self) -> str:
        return self._pack.address

    @property
    def name(self) -> str:
        return f"{self.device} {self.serial_number[-4:]}"

    @property
    def device(self) -> str:
        return (
            "STREAM 5000" if self.serial_number.startswith("ES21") else "STREAM AC 5000"
        )

    @property
    def is_connected(self) -> bool:
        return self._pack.is_connected

    @computed_field
    def unit_battery_ac_input_power(self) -> float | None:
        return ppositive()(self.unit_battery_power)

    @computed_field
    def unit_battery_ac_output_power(self) -> float | None:
        return pnegative()(self.unit_battery_power)

    @controls.outlet(ac_ports)
    async def enable_ac_ports(self, enabled: bool):
        await self._pack.send_to_unit(self.serial_number, "enable_ac_ports", enabled)

    def update_field(self, name: str, value: Any) -> None:
        if value is None:
            return
        self.reset_updated()
        setattr(self, name, value)
        self._notify_updated()

    def apply(self, report: "UnitReport") -> None:
        self.reset_updated()
        for name, value in (
            ("battery_level_main", report.soc),
            ("unit_battery_power", report.battery_power),
            ("unit_grid_power", report.grid_power),
            ("backup_port_power", report.backup_power),
        ):
            if value is not None:
                setattr(self, name, value)
        self._apply_solar(report)
        self._notify_updated()

    def _apply_solar(self, report: "UnitReport") -> None:
        pass


class SolarPackUnit(PackUnit):
    """A battery with solar inputs, from the same report the app reads them in"""

    pv_power_1 = Field[int]()
    pv_power_2 = Field[int]()
    pv_power_3 = Field[int]()
    pv_power_4 = Field[int]()

    def _apply_solar(self, report: "UnitReport") -> None:
        if report.pv_power is None:
            return
        self.pv_power_1, self.pv_power_2, self.pv_power_3, self.pv_power_4 = (
            report.pv_power
        )


def _unit_class(serial_number: str) -> type[PackUnit]:
    return SolarPackUnit if serial_number.startswith("ES21") else PackUnit


class Pack(_VirtualDevice):
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
        self.units: dict[str, PackUnit] = {}
        self.desired_report_interval = DEFAULT_REPORT_INTERVAL
        # How the host keeps what can't live here: where a unit's device is, what the
        # owner chose, and which entities a new unit needs
        self.resolve_unit_address: Callable[[str], str | None] = lambda _serial: None
        self.on_report_interval_chosen: Callable[[float], None] = lambda _seconds: None
        self.new_unit_listeners: list[Callable[[PackUnit], None]] = []
        self._members: dict[Member, list[Callable[[], None]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

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

        stops: list[Callable[[], None]] = [member.on_unit_report(self._apply_report)]
        self._members[member] = stops
        for member_field, pack_field in _MEMBER_FIELDS.items():

            def update(
                value: Any,
                pack_field: str = pack_field,
                member_field: str = member_field,
            ) -> None:
                self._update(pack_field, value)

            member.register_state_update_callback(update, member_field)
            stops.append(
                lambda update=update, member_field=member_field: (
                    member.remove_state_update_callback(update, member_field)
                )
            )
            update(getattr(member, member_field))

        def enforce(seconds: float | None) -> None:
            self._enforce_report_interval(member, seconds)

        member.register_state_update_callback(enforce, "pack_report_interval")
        stops.append(
            lambda: member.remove_state_update_callback(enforce, "pack_report_interval")
        )
        unit = self._unit(member.serial_number, member.address)
        for member_field, unit_field in _MEMBER_UNIT_FIELDS.items():

            def update_unit(value: Any, unit_field: str = unit_field) -> None:
                unit.update_field(unit_field, value)

            member.register_state_update_callback(update_unit, member_field)
            stops.append(
                lambda update_unit=update_unit, member_field=member_field: (
                    member.remove_state_update_callback(update_unit, member_field)
                )
            )
            update_unit(getattr(member, member_field))
        self._publish_all()

    def detach(self, member: "Member") -> None:
        for stop in self._members.pop(member, []):
            stop()
        self._publish_all()

    def _publish_all(self) -> None:
        self.publish_availability()
        for unit in self.units.values():
            unit.publish_availability()

    def _unit(self, serial_number: str, address: str | None = None) -> PackUnit:
        if (unit := self.units.get(serial_number)) is not None:
            return unit

        address = (
            address
            or self.resolve_unit_address(serial_number)
            or f"unit_{serial_number}"
        )
        unit = _unit_class(serial_number)(self, serial_number, address)
        self.units[serial_number] = unit
        for listener in self.new_unit_listeners:
            listener(unit)
        return unit

    def known_unit(self, serial_number: str) -> None:
        """Set up a unit from a previous run, before anything reports it"""
        self._unit(serial_number)

    def _apply_report(self, report: "UnitReport") -> None:
        self._unit(report.serial).apply(report)

    def _update(self, pack_field: str, value: Any) -> None:
        if value is None:
            return
        self.reset_updated()
        setattr(self, pack_field, value)
        self._notify_updated()

    def _enforce_report_interval(self, member: "Member", seconds: float | None) -> None:
        # A unit falls back to its own period whenever its link drops, so what the owner
        # chose has to be sent again each time it reports another one
        if seconds is None or seconds == self.desired_report_interval:
            return
        task = asyncio.create_task(
            member.set_report_interval(self.desired_report_interval)
        )
        self._tasks.add(task)
        task.add_done_callback(self._finish_task)

    def _finish_task(self, task: "asyncio.Task[Any]") -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.debug("Could not set the report interval: %s", error)

    def _by_priority(self) -> "list[Member]":
        # Roles change over the life of a pack, so read them at each command. The sort
        # is stable: units with no known role keep the order they joined in
        return sorted(
            (member for member in self._members if member.is_connected),
            key=lambda member: not member.is_master,
        )

    async def send_to_unit(self, serial_number: str, name: str, *args: Any) -> None:
        """Send what only that unit can apply, which its own link has to carry"""
        for member in self._members:
            if member.serial_number == serial_number and member.is_connected:
                await getattr(member, name)(*args)
                return
        raise NotConnectedError(f"{serial_number} is not connected")

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
        self.desired_report_interval = seconds
        self.on_report_interval_chosen(seconds)
        for member in members:
            await member.set_report_interval(seconds)
        return True

    @controls.switch(energy_backup)
    async def enable_energy_backup(self, enabled: bool):
        await self._command("enable_energy_backup", enabled)
