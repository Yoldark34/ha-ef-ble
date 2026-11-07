from typing import ClassVar

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from google.protobuf.message import Message

from ..devicebase import DeviceBase
from ..entity import controls
from ..entity.base import dynamic
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
from ..props.transforms import out_power, pnegative, ppositive, pround
from ..serial_routing import SerialRouting

pb = proto_attr_mapper(es22_bkw_pb2.DisplayPropertyUpload)

# Which `PropertyCmd` field each configuration block occupies, keyed by the block's own
# type so that a write names the block by class rather than by string
_PROPERTY_BLOCKS = {
    field.message_type.full_name: field
    for field in es22_bkw_pb2.PropertyCmd.DESCRIPTOR.fields
    if field.message_type is not None
}

pb_flow = proto_attr_mapper(es22_bkw_pb2.DevEnergyFlowDetail)
pb_battery = proto_attr_mapper(es22_bkw_pb2.BatteryInfoItem)
pb_system_battery = proto_attr_mapper(es22_bkw_pb2.SystemSubDevBatteryInfo)

# The device leaves out a reading that is zero, even inside a block it does post, where
# keeping the previous reading would hold on to a flow that has stopped
ZERO_IF_OMITTED = TransformIfMissing[int, int](lambda value: value or 0)
_ROUNDED_OR_ZERO = TransformIfMissing[float, float](
    lambda value: round(value, 2) if value is not None else 0.0
)


def _preferred[T](value: T | None, fallback: T | None) -> T | None:
    return value if value is not None else fallback


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
    _WRITE_CMD_SET: ClassVar[int] = 0xFE
    _WRITE_CMD_ID: ClassVar[int] = 0x26
    _SYSTEM_MODULE: ClassVar[int] = 0x02

    system_role = pb_field(
        pb.system_config.distributed_device_role, SystemRole.from_value
    )
    system_unit_count = pb_field(pb.system_config.system_dev_cnt)

    # Each unit of a pack posts both `system_sub_dev_*` blocks with an entry per unit, and
    # `sys_energy_stream_report` with the totals of the pack. A unit reads its own entry
    # and falls back on those totals only while the blocks arrive empty
    _sub_dev_battery_level = repeated_pb_field(
        pb.system_sub_dev_battery_report.system_sub_dev_battery_info,
        pb_system_battery.soc,
        own_key=pb_system_battery.sn,
    )
    _stream_battery_level = pb_field(pb.sys_energy_stream_report.bp_soc)
    _stream_battery_power = pb_field(
        pb.sys_energy_stream_report.bp_pwr, ZERO_IF_OMITTED
    )
    _stream_grid_power = pb_field(
        pb.sys_energy_stream_report.on_grid_pwr, ZERO_IF_OMITTED
    )
    battery_level_main = repeated_pb_field(
        pb.battery_info_report.battery_info_item,
        pb_battery.soc,
        own_key=pb_battery.sn,
    )

    load_system = pb_field(pb.sys_energy_stream_report.load_pwr)
    remaining_time_charging = pb_field(pb.sys_battery_info_report.charge_remain_time)
    remaining_time_discharging = pb_field(
        pb.sys_battery_info_report.discharge_remain_time
    )
    battery_full_capacity = pb_field(pb.sys_battery_info_report.full_cap)
    battery_remaining_energy = pb_field(pb.sys_battery_info_report.remain_cap)

    _sub_dev_battery_power = repeated_pb_field(
        pb.system_sub_dev_energy_flow_detail.dev_energy_flow_detail,
        pb_flow.inv_pwr,
        TransformIfMissing[float, float](
            lambda value: out_power(value) if value is not None else 0.0
        ),
        own_key=pb_flow.dev_sn,
    )
    _sub_dev_battery_ac_power = repeated_pb_field(
        pb.system_sub_dev_energy_flow_detail.dev_energy_flow_detail,
        pb_flow.bp_pwr,
        _ROUNDED_OR_ZERO,
        own_key=pb_flow.dev_sn,
    )
    _sub_dev_grid_power = repeated_pb_field(
        pb.system_sub_dev_energy_flow_detail.dev_energy_flow_detail,
        pb_flow.on_grid_pwr,
        _ROUNDED_OR_ZERO,
        own_key=pb_flow.dev_sn,
    )
    _backup_pwr = repeated_pb_field(
        pb.system_sub_dev_energy_flow_detail.dev_energy_flow_detail,
        pb_flow.backup_pwr,
        pround(2),
        own_key=pb_flow.dev_sn,
    )
    # No capture carries either of these yet, so reading their absence as zero assumes
    # they follow the other flows of this block
    _bp_to_backup = pb_field(
        pb.energy_flow_from_to_detail.bp_to_backup, ZERO_IF_OMITTED
    )
    _grid_to_backup = pb_field(
        pb.energy_flow_from_to_detail.grid_to_backup, ZERO_IF_OMITTED
    )

    # What the whole house draws from the grid, where `grid_power` is the unit's own
    # grid port: a unit charging at 1002 W under a 200 W load reports 1202 and -1002
    house_grid_power = pb_field(pb.sys_energy_stream_report.grid_pwr, ZERO_IF_OMITTED)

    load_from_battery = pb_field(
        pb.energy_flow_from_to_detail.bp_to_load, ZERO_IF_OMITTED
    )
    load_from_grid = pb_field(
        pb.energy_flow_from_to_detail.grid_to_load, ZERO_IF_OMITTED
    )

    battery_charge_limit_max = pb_field(pb.bp_soc_config.charge_soc_upper_limit)
    battery_charge_limit_min = pb_field(pb.bp_soc_config.discharge_soc_lower_limit)
    energy_backup = pb_field(pb.bp_backup_config.backup_soc_on_off)
    energy_backup_battery_level = pb_field(pb.bp_backup_config.backup_soc)
    ac_ports = pb_field(pb.dev_switch.ac_socket_onoff)

    cell_temperature = repeated_pb_field(
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

    @classmethod
    def check(cls, sn):
        return sn[:4] in cls.SN_PREFIX

    @computed_field
    def battery_level(self) -> int | None:
        return _preferred(self._sub_dev_battery_level, self._stream_battery_level)

    @computed_field
    def battery_power(self) -> float | None:
        return _preferred(self._sub_dev_battery_power, self._stream_battery_power)

    @computed_field
    def battery_ac_input_power(self) -> float | None:
        return ppositive()(self._battery_ac_power)

    @computed_field
    def battery_ac_output_power(self) -> float | None:
        return pnegative()(self._battery_ac_power)

    @computed_field
    def grid_power(self) -> float | None:
        return _preferred(self._sub_dev_grid_power, self._stream_grid_power)

    @property
    def _battery_ac_power(self) -> float | None:
        return _preferred(self._sub_dev_battery_ac_power, self._stream_battery_power)

    @computed_field
    def backup_port_power(self) -> float | None:
        if self._backup_pwr is not None:
            return self._backup_pwr
        if self._bp_to_backup is None and self._grid_to_backup is None:
            return None
        return (self._bp_to_backup or 0) + (self._grid_to_backup or 0)

    async def packet_parse(self, data: bytes):
        return Packet.from_bytes(data, xor_payload=True)

    async def data_parse(self, packet: Packet) -> bool:
        processed = False
        self.reset_updated()

        if isinstance(packet, PacketV4):
            processed = self._parse_routed(packet)
        elif (packet.src, packet.cmd_set, packet.cmd_id) == (
            self._SYSTEM_MODULE,
            self._WRITE_CMD_SET,
            self._TELEMETRY_CMD_ID,
        ):
            self.update_from_bytes(es22_bkw_pb2.DisplayPropertyUpload, packet.payload)
            processed = True

        self._notify_updated()

        return processed

    def _parse_routed(self, packet: PacketV4) -> bool:
        envelope = SerialRouting.envelope(packet.payload)
        if envelope is None or (envelope.src, envelope.cmd_set, envelope.cmd_id) != (
            self._SYSTEM_MODULE,
            self._WRITE_CMD_SET,
            self._TELEMETRY_CMD_ID,
        ):
            return False

        self._routing.remember_post(packet)
        _, body = SerialRouting.split(packet.payload)
        self.update_from_bytes(es22_bkw_pb2.DisplayPropertyUpload, body)
        return True

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

    @controls.battery(
        battery_charge_limit_max,
        min=dynamic(battery_charge_limit_min),
    )
    async def set_battery_charge_limit_max(self, limit: float):
        await self._write_property(
            es22_bkw_pb2.BpSocConfig(charge_soc_upper_limit=int(limit))
        )
        return True

    @controls.battery(
        battery_charge_limit_min,
        max=dynamic(battery_charge_limit_max),
    )
    async def set_battery_charge_limit_min(self, limit: float):
        await self._write_property(
            es22_bkw_pb2.BpSocConfig(discharge_soc_lower_limit=int(limit))
        )
        return True

    @controls.battery(
        energy_backup_battery_level,
        min=dynamic(battery_charge_limit_min),
        max=dynamic(battery_charge_limit_max),
    )
    async def set_energy_backup_battery_level(self, value: float):
        await self._write_property(es22_bkw_pb2.BpBackupConfig(backup_soc=int(value)))
        return True

    @controls.switch(energy_backup)
    async def enable_energy_backup(self, enabled: bool):
        await self._write_property(
            es22_bkw_pb2.BpBackupConfig(backup_soc_on_off=enabled)
        )

    @controls.outlet(ac_ports)
    async def enable_ac_ports(self, enabled: bool):
        await self._write_property(es22_bkw_pb2.DevSwitch(ac_socket_onoff=enabled))
