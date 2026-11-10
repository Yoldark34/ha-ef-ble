import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from google.protobuf.message import Message

from ..connection import ConnectionState
from ..devicebase import DeviceBase
from ..packet import Packet, PacketV4
from ..pb import es22_bkw_pb2
from ..props import (
    ProtobufProps,
    computed_field,
    pb_field,
    proto_attr_mapper,
    repeated_pb_field,
)
from ..props.enums import IntFieldValue
from ..props.protobuf_field import TransformIfMissing
from ..serial_routing import SerialRouting

pb = proto_attr_mapper(es22_bkw_pb2.DisplayPropertyUpload)
# The runtime report that carries the upload periods has no message of its own in the
# schema, but keeps them at the field number `PropertyCmd` writes them to
pb_periods = proto_attr_mapper(es22_bkw_pb2.PropertyCmd)

# Which `PropertyCmd` field each configuration block occupies, keyed by the block's own
# type so that a write names the block by class rather than by string
_PROPERTY_BLOCKS = {
    field.message_type.full_name: field
    for field in es22_bkw_pb2.PropertyCmd.DESCRIPTOR.fields
    if field.message_type is not None
}

pb_battery = proto_attr_mapper(es22_bkw_pb2.BatteryInfoItem)

# The device leaves out a reading that is zero, even inside a block it does post, where
# keeping the previous reading would hold on to a flow that has stopped
ZERO_IF_OMITTED = TransformIfMissing[int, int](lambda value: value or 0)


@dataclass(frozen=True, kw_only=True)
class UnitReport:
    """What a post says of one unit, which is not always the one that sent it"""

    serial: str
    soc: int | None = None
    battery_power: int | None = None
    grid_power: int | None = None
    backup_power: int | None = None
    pv_power: tuple[int, int, int, int] | None = None


class WorkMode(IntFieldValue):
    UNKNOWN = -1

    SELF_CONSUMPTION = 0
    AI_MODE = 1
    TIMER_TASK = 2
    POU_MODE = 3
    EMERGENCY_STOP = 4


class SystemRole(IntFieldValue):
    UNKNOWN = -1

    ELECTION = 0
    MASTER = 1
    SLAVE = 2


class Device(DeviceBase, ProtobufProps):
    """STREAM AC 5000"""

    SN_PREFIX = (b"ES22",)
    NAME_PREFIX = "EF-6"

    _TELEMETRY_CMD_ID: ClassVar[int] = 0x27
    _RUNTIME_CMD_ID: ClassVar[int] = 0x28
    _WRITE_CMD_SET: ClassVar[int] = 0xFE
    _WRITE_CMD_ID: ClassVar[int] = 0x26
    _SYSTEM_MODULE: ClassVar[int] = 0x02

    # A unit sends several frames a second while its link is up and its telemetry at
    # least every two minutes, so this long without a frame means the link is dead even
    # though BlueZ still reports it connected, and nothing else would notice
    _SILENCE_LIMIT: ClassVar[float] = 300
    _SILENCE_CHECK_INTERVAL: ClassVar[float] = 60

    # The `own_` fields are this unit's alone and the master does not relay them, so
    # they need this unit's link. `eflib.pack.PackUnit` reads them and owns their entities
    own_system_role = pb_field(
        pb.system_config.distributed_device_role, SystemRole.from_value
    )
    system_group_id = pb_field(pb.system_config.system_group_id)

    # The whole pack, repeated identically by each unit. The `pack_` names keep these off
    # the unit's own device: `eflib.pack.Pack` reads them and owns their entities
    pack_unit_count = pb_field(pb.system_config.system_dev_cnt)
    pack_work_mode = pb_field(pb.work_mode, WorkMode.from_value)
    pack_battery_level = pb_field(pb.sys_energy_stream_report.bp_soc)
    pack_battery_power = pb_field(pb.sys_energy_stream_report.bp_pwr, ZERO_IF_OMITTED)
    pack_grid_power = pb_field(pb.sys_energy_stream_report.on_grid_pwr, ZERO_IF_OMITTED)
    # What the whole house draws from the grid, where `pack_grid_power` is the pack's
    # own grid port: a pack charging at 1002 W under a 200 W load reports 1202 and -1002
    pack_house_grid_power = pb_field(
        pb.sys_energy_stream_report.grid_pwr, ZERO_IF_OMITTED
    )
    pack_load_system = pb_field(pb.sys_energy_stream_report.load_pwr)
    pack_load_from_battery = pb_field(
        pb.energy_flow_from_to_detail.bp_to_load, ZERO_IF_OMITTED
    )
    pack_load_from_grid = pb_field(
        pb.energy_flow_from_to_detail.grid_to_load, ZERO_IF_OMITTED
    )
    pack_battery_charge_limit_max = pb_field(pb.bp_soc_config.charge_soc_upper_limit)
    pack_battery_charge_limit_min = pb_field(pb.bp_soc_config.discharge_soc_lower_limit)
    pack_energy_backup = pb_field(pb.bp_backup_config.backup_soc_on_off)
    pack_energy_backup_battery_level = pb_field(pb.bp_backup_config.backup_soc)

    pack_feed_grid_pow_limit = pb_field(
        pb.power_parameter_config.on_grid_feed_power_limit
    )
    pack_feed_grid_pow_max = pb_field(pb.power_parameter_config.on_grid_feed_power_max)
    pack_grid_in_power_limit = pb_field(
        pb.power_parameter_config.on_grid_take_power_limit
    )

    pack_report_interval = pb_field(
        pb_periods.property_upload_period.display_property_full_upload_period,
        lambda milliseconds: milliseconds / 1000,
    )
    _display_incremental_period = pb_field(
        pb_periods.property_upload_period.display_property_incremental_upload_period
    )
    _runtime_full_period = pb_field(
        pb_periods.property_upload_period.runtime_property_full_upload_period
    )
    _runtime_incremental_period = pb_field(
        pb_periods.property_upload_period.runtime_property_incremental_upload_period
    )

    own_remaining_time_charging = pb_field(
        pb.sys_battery_info_report.charge_remain_time
    )
    own_remaining_time_discharging = pb_field(
        pb.sys_battery_info_report.discharge_remain_time
    )
    own_battery_full_capacity = pb_field(pb.sys_battery_info_report.full_cap)
    own_battery_remaining_energy = pb_field(pb.sys_battery_info_report.remain_cap)
    own_ac_ports = pb_field(pb.dev_switch.ac_socket_onoff)
    # X-Boost and UPS live in `SysDevInfo` (fields 5/6), which the device also reports in
    # `DisplayPropertyUpload`, so each reads back as well as it writes
    own_xboost = pb_field(pb.sys_dev_info.x_boost_on_off)
    own_ups = pb_field(pb.sys_dev_info.ups_on_off)

    own_cell_temperature = repeated_pb_field(
        pb.battery_info_report.battery_info_item,
        pb_battery.cell_max_temp,
        own_key=pb_battery.sn,
    )

    def __init__(
        self, ble_dev: BLEDevice, adv_data: AdvertisementData, sn: str
    ) -> None:
        super().__init__(ble_dev, adv_data, sn)
        self._routing = SerialRouting(
            sn,
            cmd_set=self._WRITE_CMD_SET,
            cmd_id=self._WRITE_CMD_ID,
            dst=self._SYSTEM_MODULE,
            fallback_dst=self._SYSTEM_MODULE,
            sequenced_writes=True,
        )
        self._unit_report_listeners: list[Callable[[UnitReport], None]] = []
        self._last_frame_at = time.monotonic()
        self.on_connection_state_change(self._restart_silence_clock)
        self.add_timer_task(
            self._drop_silent_link, interval=self._SILENCE_CHECK_INTERVAL
        )

    @classmethod
    def check(cls, sn):
        return sn[:4] in cls.SN_PREFIX

    @property
    def is_master(self) -> bool:
        return self.own_system_role is SystemRole.MASTER

    @computed_field
    def pack_id(self) -> str | None:
        """Which pack this unit belongs to, known once it has reported its system"""
        if self.system_group_id:
            return str(self.system_group_id)
        return self.serial_number if self.pack_unit_count is not None else None

    async def packet_parse(self, data: bytes):
        return Packet.from_bytes(data, xor_payload=True)

    def _restart_silence_clock(self, state: ConnectionState) -> None:
        if state.authenticated:
            self._last_frame_at = time.monotonic()

    async def _drop_silent_link(self) -> None:
        silent_for = time.monotonic() - self._last_frame_at
        if silent_for < self._SILENCE_LIMIT or self._conn is None:
            return
        self._logger.warning(
            "No frame from the device for %d s, dropping the link to reconnect",
            silent_for,
        )
        self._conn.disconnected()

    async def data_parse(self, packet: Packet) -> bool:
        self._last_frame_at = time.monotonic()
        processed = False
        self.reset_updated()

        if isinstance(packet, PacketV4):
            processed = self._parse_routed(packet)
        elif (packet.src, packet.cmd_set, packet.cmd_id) == (
            self._SYSTEM_MODULE,
            self._WRITE_CMD_SET,
            self._TELEMETRY_CMD_ID,
        ):
            self._apply_telemetry(packet.payload)
            processed = True

        self._notify_updated()

        return processed

    def _parse_routed(self, packet: PacketV4) -> bool:
        envelope = SerialRouting.envelope(packet.payload)
        if envelope is None or (envelope.src, envelope.cmd_set) != (
            self._SYSTEM_MODULE,
            self._WRITE_CMD_SET,
        ):
            return False

        _, body = SerialRouting.split(packet.payload)
        if envelope.cmd_id == self._RUNTIME_CMD_ID:
            self.update_from_bytes(es22_bkw_pb2.PropertyCmd, body)
            return True
        if envelope.cmd_id != self._TELEMETRY_CMD_ID:
            return False

        self._routing.remember_post(packet)
        self._apply_telemetry(body)
        return True

    def _apply_telemetry(self, body: bytes) -> None:
        message = self.update_from_bytes(es22_bkw_pb2.DisplayPropertyUpload, body)
        if message is not None:
            self._publish_unit_reports(message)

    def on_unit_report(
        self, listener: Callable[[UnitReport], None]
    ) -> Callable[[], None]:
        """Listen to what posts say of each unit, and return how to stop"""
        self._unit_report_listeners.append(listener)
        return lambda: self._unit_report_listeners.remove(listener)

    def _publish_unit_reports(
        self, message: es22_bkw_pb2.DisplayPropertyUpload
    ) -> None:
        for report in self._unit_reports(message):
            for listener in self._unit_report_listeners:
                listener(report)

    def _unit_reports(
        self, message: es22_bkw_pb2.DisplayPropertyUpload
    ) -> list[UnitReport]:
        # A unit reports itself in `dev_energy_stream_report`, and the master also lists
        # every unit in `system_sub_dev_energy_flow_detail`. Both leave out what is zero
        reports = []
        if message.HasField("dev_energy_stream_report"):
            own = message.dev_energy_stream_report
            reports.append(
                UnitReport(
                    serial=self.serial_number,
                    battery_power=own.bp_pwr,
                    grid_power=own.on_grid_pwr,
                    backup_power=own.backup_pwr,
                    pv_power=(own.pv1_pwr, own.pv2_pwr, own.pv3_pwr, own.pv4_pwr),
                )
            )
        detail = message.system_sub_dev_energy_flow_detail
        reports.extend(
            UnitReport(
                serial=unit.dev_sn,
                soc=round(unit.dev_soc),
                battery_power=round(unit.bp_pwr),
                grid_power=round(unit.on_grid_pwr),
                backup_power=round(unit.backup_pwr),
                pv_power=(
                    round(unit.pv_1_pwr),
                    round(unit.pv_2_pwr),
                    round(unit.pv_3_pwr),
                    round(unit.pv_4_pwr),
                ),
            )
            for unit in detail.dev_energy_flow_detail
        )
        reports.extend(
            UnitReport(serial=battery.sn, soc=battery.soc)
            for battery in message.battery_info_report.battery_info_item
            if battery.sn
        )
        return reports

    async def _write_property(self, config: Message) -> None:
        # `action_id` selects which of the optional blocks the device applies, and is
        # the field number that block occupies
        field = _PROPERTY_BLOCKS.get(config.DESCRIPTOR.full_name)
        if field is None:
            raise TypeError(
                f"{config.DESCRIPTOR.name} is not a configuration block of PropertyCmd"
            )

        message = es22_bkw_pb2.PropertyCmd(action_id=field.number)
        getattr(message, field.name).CopyFrom(config)

        packet = self._routing.write_packet(message.SerializeToString())
        await self.send_packet(packet, raise_on_failure=True)

    # The settings below belong to the pack and any unit applies them to all of them, so
    # `eflib.pack.Pack` exposes the controls and calls these

    async def set_battery_charge_limit_max(self, limit: float):
        await self._write_property(
            es22_bkw_pb2.BpSocConfig(charge_soc_upper_limit=int(limit))
        )
        return True

    async def set_battery_charge_limit_min(self, limit: float):
        await self._write_property(
            es22_bkw_pb2.BpSocConfig(discharge_soc_lower_limit=int(limit))
        )
        return True

    async def set_energy_backup_battery_level(self, value: float):
        await self._write_property(es22_bkw_pb2.BpBackupConfig(backup_soc=int(value)))
        return True

    async def set_feed_grid_pow_limit(self, value: float):
        await self._write_property(
            es22_bkw_pb2.BkwPowerParameterConfig(on_grid_feed_power_limit=int(value))
        )
        return True

    async def set_grid_in_power_limit(self, value: float):
        await self._write_property(
            es22_bkw_pb2.BkwPowerParameterConfig(on_grid_take_power_limit=int(value))
        )
        return True

    async def set_work_mode(self, mode: WorkMode):
        # Unlike the other settings this one is a plain value in `PropertyCmd`, not a
        # block, and `action_id` is the number of its field all the same
        field = es22_bkw_pb2.PropertyCmd.DESCRIPTOR.fields_by_name["work_mode"]
        message = es22_bkw_pb2.PropertyCmd(action_id=field.number, work_mode=mode.value)
        packet = self._routing.write_packet(message.SerializeToString())
        await self.send_packet(packet, raise_on_failure=True)

    @property
    def knows_report_periods(self) -> bool:
        return None not in (
            self._display_incremental_period,
            self._runtime_full_period,
            self._runtime_incremental_period,
        )

    async def set_report_interval(self, seconds: float):
        # The block is written whole, so the other periods have to go out as they are
        if not self.knows_report_periods:
            raise ValueError(f"{self.name}: upload periods are not known yet")

        await self._write_property(
            es22_bkw_pb2.PropertyUploadPeriod(
                display_property_full_upload_period=int(seconds * 1000),
                display_property_incremental_upload_period=(
                    self._display_incremental_period
                ),
                runtime_property_full_upload_period=self._runtime_full_period,
                runtime_property_incremental_upload_period=(
                    self._runtime_incremental_period
                ),
            )
        )
        return True

    async def enable_energy_backup(self, enabled: bool):
        await self._write_property(
            es22_bkw_pb2.BpBackupConfig(backup_soc_on_off=enabled)
        )

    async def enable_ac_ports(self, enabled: bool):
        await self._write_property(es22_bkw_pb2.DevSwitch(ac_socket_onoff=enabled))

    async def enable_xboost(self, enabled: bool):
        # `SysDevInfo` carries the app's clock in fields 1/2; writing only the toggle
        # leaves those untouched, which is what the app does
        await self._write_property(es22_bkw_pb2.SysDevInfo(x_boost_on_off=enabled))

    async def enable_ups(self, enabled: bool):
        await self._write_property(es22_bkw_pb2.SysDevInfo(ups_on_off=enabled))
