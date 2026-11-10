"""A pack of linked STREAM units, seen as one device"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bleak.exc import BleakError

from .devices.stream_ac_5000 import WorkMode
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
    "pack_work_mode": "work_mode",
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
    "own_xboost": "xboost",
    "own_ups": "ups",
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


# These mostly repeat another sensor of the pack, so they start disabled
PACK_KEYS_OFF_BY_DEFAULT = frozenset(
    {"grid_power", "load_from_battery", "load_from_grid"}
)


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
    xboost = Field[bool]()
    ups = Field[bool]()
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
    def has_bluetooth_address(self) -> bool:
        return not self.address.startswith("unit_")

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

    @controls.switch(xboost)
    async def enable_xboost(self, enabled: bool):
        await self._pack.send_to_unit(self.serial_number, "enable_xboost", enabled)

    @controls.switch(ups)
    async def enable_ups(self, enabled: bool):
        await self._pack.send_to_unit(self.serial_number, "enable_ups", enabled)

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


@dataclass(frozen=True)
class StandbyLimits:
    """The limits a pack had when it went into standby, to go back to"""

    feed_grid_pow_limit: int
    grid_in_power_limit: int


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
    pack_standby = Field[bool]()
    work_mode = Field[WorkMode]()

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
        self.on_standby_changed: Callable[[StandbyLimits | None], None] = (
            lambda _limits: None
        )
        self.new_unit_listeners: list[Callable[[PackUnit], None]] = []
        self.pack_standby = False
        self._standby_limits: StandbyLimits | None = None
        self._standby_confirmed = False
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
        if pack_field in ("feed_grid_pow_limit", "grid_in_power_limit"):
            self._watch_standby()

    def _show(self, pack_field: str, value: Any) -> None:
        # A unit only echoes a setting in its next report, up to an interval later, so
        # this shows what was just accepted, as the app does. Not through `_update`: it
        # says nothing of what the units hold, which standby needs to tell apart
        self.reset_updated()
        setattr(self, pack_field, value)
        self._notify_updated()

    def restore_standby(self, limits: StandbyLimits) -> None:
        """Pick up a standby from a previous run, already applied to the units"""
        self._standby_limits = limits
        self._standby_confirmed = True
        self.reset_updated()
        self.pack_standby = True
        self._notify_updated()

    def _watch_standby(self) -> None:
        if self._standby_limits is None:
            return
        limits = (self.feed_grid_pow_limit, self.grid_in_power_limit)
        # After a restart the two arrive one at a time, and an unknown one says nothing
        if None in limits:
            return
        if not self._standby_confirmed:
            # The units take a report to echo the zeros, so what came before them says
            # nothing about what anyone else did
            self._standby_confirmed = limits == (0, 0)
            return
        if limits != (0, 0):
            # Whoever changed them chose what the pack does now, so nothing is written
            # back: only turning standby off in Home Assistant restores the old values
            _LOGGER.debug("Limits changed outside of standby, leaving it")
            self._set_standby(None)

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._finish_task)

    def _enforce_report_interval(self, member: "Member", seconds: float | None) -> None:
        # A unit falls back to its own period whenever its link drops, so what the owner
        # chose has to be sent again each time it reports another one
        if seconds is None or seconds == self.desired_report_interval:
            return
        self._spawn(member.set_report_interval(self.desired_report_interval))

    def _finish_task(self, task: "asyncio.Task[Any]") -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.warning("Pack command failed: %s", error)

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
        self._show("battery_charge_limit_max", int(limit))
        return True

    @controls.battery(battery_charge_limit_min, max=dynamic(battery_charge_limit_max))
    async def set_battery_charge_limit_min(self, limit: float):
        await self._command("set_battery_charge_limit_min", limit)
        self._show("battery_charge_limit_min", int(limit))
        return True

    @controls.battery(
        energy_backup_battery_level,
        min=dynamic(battery_charge_limit_min),
        max=dynamic(battery_charge_limit_max),
    )
    async def set_energy_backup_battery_level(self, value: float):
        await self._command("set_energy_backup_battery_level", value)
        self._show("energy_backup_battery_level", int(value))
        return True

    def _refuse_during_standby(self) -> None:
        if self.pack_standby:
            raise ValueError("The pack is in standby, turn it off to change this limit")

    @controls.power(feed_grid_pow_limit, max=dynamic(feed_grid_pow_max))
    async def set_feed_grid_pow_limit(self, value: float):
        self._refuse_during_standby()
        await self._command("set_feed_grid_pow_limit", value)
        self._show("feed_grid_pow_limit", int(value))
        return True

    @controls.power(grid_in_power_limit, max=_GRID_IN_POWER_MAX)
    async def set_grid_in_power_limit(self, value: float):
        self._refuse_during_standby()
        await self._command("set_grid_in_power_limit", value)
        self._show("grid_in_power_limit", int(value))
        return True

    def _set_standby(self, limits: StandbyLimits | None) -> None:
        self._standby_limits = limits
        self._standby_confirmed = False
        self.on_standby_changed(limits)
        self.reset_updated()
        self.pack_standby = limits is not None
        self._notify_updated()

    async def _write_limits(self, feed: int, grid_in: int) -> None:
        await self._command("set_feed_grid_pow_limit", feed)
        self._show("feed_grid_pow_limit", feed)
        await self._command("set_grid_in_power_limit", grid_in)
        self._show("grid_in_power_limit", grid_in)

    # The two left out are not offered in the app's mode list, but stay readable
    @controls.select(
        work_mode,
        options=controls.option_set(WorkMode).exclude(
            WorkMode.POU_MODE, WorkMode.EMERGENCY_STOP
        ),
    )
    async def set_work_mode(self, mode: WorkMode):
        await self._command("set_work_mode", mode)
        self._show("work_mode", mode)

    @controls.switch(pack_standby)
    async def enable_pack_standby(self, enabled: bool):
        if enabled:
            await self._enter_standby()
        else:
            await self._leave_standby()

    async def _enter_standby(self) -> None:
        if self.pack_standby:
            return
        if self.feed_grid_pow_limit is None or self.grid_in_power_limit is None:
            raise ValueError("The pack's limits are not known yet")

        limits = StandbyLimits(self.feed_grid_pow_limit, self.grid_in_power_limit)
        try:
            await self._write_limits(0, 0)
        except NotConnectedError:
            # One limit may have gone through, and a pack half in standby is the worst of
            # both, so put it back before reporting the failure
            await self._put_limits_back(limits)
            raise
        self._set_standby(limits)

    async def _put_limits_back(self, limits: StandbyLimits) -> None:
        try:
            await self._write_limits(
                limits.feed_grid_pow_limit, limits.grid_in_power_limit
            )
        except NotConnectedError:
            _LOGGER.warning(
                "Could not restore the pack's limits to %s W and %s W",
                limits.feed_grid_pow_limit,
                limits.grid_in_power_limit,
            )

    async def _leave_standby(self) -> None:
        if (limits := self._standby_limits) is None:
            return
        await self._write_limits(limits.feed_grid_pow_limit, limits.grid_in_power_limit)
        self._set_standby(None)

    @controls.duration(report_interval, min=10, max=120, step=5)
    async def set_report_interval(self, seconds: float):
        # Each unit keeps its own, so unlike the pack's settings this goes to all of them
        members = [member for member in self._members if member.is_connected]
        if not members:
            raise NotConnectedError("No unit of the pack can be reached")
        self.desired_report_interval = seconds
        self.on_report_interval_chosen(seconds)
        # A unit that has not reported its periods yet gets this once it does
        for member in members:
            if member.knows_report_periods:
                await member.set_report_interval(seconds)
        self._show("report_interval", seconds)
        return True

    @controls.switch(energy_backup)
    async def enable_energy_backup(self, enabled: bool):
        await self._command("enable_energy_backup", enabled)
        self._show("energy_backup", enabled)
