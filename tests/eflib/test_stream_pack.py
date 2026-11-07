import asyncio

import pytest
from bleak.exc import BleakError
from pytest_mock import MockerFixture

from custom_components.ef_ble.eflib.devices import stream_5000, stream_ac_5000
from custom_components.ef_ble.eflib.devices.stream_ac_5000 import WorkMode
from custom_components.ef_ble.eflib.exceptions import NotConnectedError
from custom_components.ef_ble.eflib.pack import Pack, PackStatus, StandbyLimits
from custom_components.ef_ble.eflib.packet import Packet
from custom_components.ef_ble.eflib.pb import es22_bkw_pb2

MASTER = "da01021001"
SLAVE = "da01021002"
# A captured post of the pack's totals, with the house at 887 W and the battery idle
TOTALS = "5a0808ee0d10ee0d283a620330f706"
# A two unit pack with its reserve on at 58 %
FULL_REPORT = "da01022002f201040801103a"


def _telemetry(payload_hex: str) -> Packet:
    return Packet(0x02, 0x21, 0xFE, 0x27, bytes.fromhex(payload_hex), 0x01, 0x01, 0x03)


def _unit(mocker: MockerFixture, device_class, serial: str):
    ble_dev = mocker.Mock()
    ble_dev.address = "AA:BB:CC:DD:EE:FF"
    device = device_class(ble_dev, mocker.MagicMock(), serial)
    device._conn = mocker.AsyncMock()
    device._conn.is_connected = True
    return device


@pytest.fixture
async def master(mocker):
    unit = _unit(mocker, stream_ac_5000.Device, "ES22XXXXXXXXX447")
    await unit.data_parse(_telemetry(MASTER))
    return unit


@pytest.fixture
async def slave(mocker):
    unit = _unit(mocker, stream_5000.Device, "ES21XXXXXXXXX180")
    await unit.data_parse(_telemetry(SLAVE))
    return unit


@pytest.fixture
def pack(master, slave):
    pack = Pack("1", "ES22XXXXXXXXX447")
    # the slave first, so that the order a test sees comes from the roles
    pack.attach(slave)
    pack.attach(master)
    return pack


async def test_pack_reports_the_totals_any_unit_posts(pack, slave):
    await slave.data_parse(_telemetry(TOTALS))

    assert pack.battery_level == 58
    assert pack.house_grid_power == 887
    assert pack.battery_ac_input_power == 0


async def test_pack_sends_a_command_to_the_master(pack, master, slave):
    await pack.set_energy_backup_battery_level(60)

    master._conn.send_packet.assert_awaited_once()
    slave._conn.send_packet.assert_not_awaited()


@pytest.mark.parametrize("error", [NotConnectedError("gone"), BleakError("gone")])
async def test_pack_falls_back_on_a_slave_when_the_master_fails(
    pack, master, slave, error
):
    master._conn.send_packet.side_effect = error

    await pack.set_energy_backup_battery_level(60)

    slave._conn.send_packet.assert_awaited_once()


async def test_pack_skips_a_master_that_is_not_connected(pack, master, slave):
    master._conn = None

    await pack.enable_energy_backup(True)

    slave._conn.send_packet.assert_awaited_once()


async def test_pack_follows_a_change_of_master(pack, master, slave):
    await master.data_parse(_telemetry(SLAVE))
    await slave.data_parse(_telemetry(MASTER))

    await pack.set_battery_charge_limit_max(90)

    slave._conn.send_packet.assert_awaited_once()
    master._conn.send_packet.assert_not_awaited()


async def test_pack_sends_its_power_limits_to_the_master(pack, master, slave):
    await pack.set_grid_in_power_limit(2000)
    await pack.set_feed_grid_pow_limit(600)

    assert master._conn.send_packet.await_count == 2
    slave._conn.send_packet.assert_not_awaited()


async def test_pack_only_commands_its_own_units(mocker, pack, master, slave):
    other_unit = _unit(mocker, stream_5000.Device, "ES21XXXXXXXXX999")
    await other_unit.data_parse(_telemetry(MASTER))
    other_pack = Pack("2", "ES21XXXXXXXXX999")
    other_pack.attach(other_unit)

    await other_pack.set_energy_backup_battery_level(60)

    other_unit._conn.send_packet.assert_awaited_once()
    master._conn.send_packet.assert_not_awaited()
    slave._conn.send_packet.assert_not_awaited()


async def test_pack_ignores_the_totals_of_another_pack(mocker, pack):
    other_unit = _unit(mocker, stream_5000.Device, "ES21XXXXXXXXX999")
    Pack("2", "ES21XXXXXXXXX999").attach(other_unit)

    await other_unit.data_parse(_telemetry(TOTALS))

    assert pack.house_grid_power is None


async def test_pack_is_waiting_until_a_unit_has_reported(pack):
    assert pack.pack_status is PackStatus.WAITING


async def test_pack_is_ready_once_every_unit_is_connected_and_reported(
    pack, master, slave
):
    await master.data_parse(_telemetry(FULL_REPORT))

    assert pack.pack_status is PackStatus.READY


async def test_pack_is_degraded_when_a_unit_is_missing(pack, master, slave):
    await master.data_parse(_telemetry(FULL_REPORT))
    pack.detach(slave)

    assert pack.pack_status is PackStatus.DEGRADED


async def test_pack_is_offline_without_any_unit(pack, master, slave):
    pack.detach(slave)
    pack.detach(master)

    assert pack.pack_status is PackStatus.OFFLINE


def _master_list(*entries: dict) -> str:
    message = es22_bkw_pb2.DisplayPropertyUpload()
    for entry in entries:
        message.system_sub_dev_energy_flow_detail.dev_energy_flow_detail.add(**entry)
    return message.SerializeToString().hex()


async def test_pack_fills_a_unit_from_the_master_alone(pack, master, slave):
    # the shape of a real post: the slave working, the master idle
    slave._conn = None
    await master.data_parse(
        _telemetry(
            _master_list(
                {"dev_sn": master.serial_number, "dev_soc": 59.0},
                {
                    "dev_sn": slave.serial_number,
                    "dev_soc": 58.0,
                    "bp_pwr": -106.0,
                    "on_grid_pwr": 106.0,
                },
            )
        )
    )

    unit = pack.units[slave.serial_number]
    assert unit.battery_level_main == 58
    assert unit.unit_battery_power == -106
    assert unit.unit_battery_ac_output_power == 106
    assert pack.units[master.serial_number].unit_battery_power == 0


async def test_pack_unit_takes_the_latest_report_of_either_source(pack, master, slave):
    entry = {"dev_sn": slave.serial_number, "bp_pwr": -106.0}
    await master.data_parse(_telemetry(_master_list(entry)))

    own = es22_bkw_pb2.DisplayPropertyUpload()
    own.dev_energy_stream_report.bp_pwr = -80
    await slave.data_parse(_telemetry(own.SerializeToString().hex()))
    assert pack.units[slave.serial_number].unit_battery_power == -80

    await master.data_parse(_telemetry(_master_list(entry)))
    assert pack.units[slave.serial_number].unit_battery_power == -106


async def test_pack_works_the_same_when_the_other_unit_is_master(pack, master, slave):
    await slave.data_parse(_telemetry(MASTER))
    await master.data_parse(_telemetry(SLAVE))
    await slave.data_parse(
        _telemetry(_master_list({"dev_sn": master.serial_number, "bp_pwr": 200.0}))
    )

    assert pack.units[master.serial_number].unit_battery_power == 200


async def test_pack_reports_the_solar_inputs_of_a_stream_5000(pack, slave):
    own = es22_bkw_pb2.DisplayPropertyUpload()
    own.dev_energy_stream_report.pv1_pwr = 321
    own.dev_energy_stream_report.pv3_pwr = 45
    await slave.data_parse(_telemetry(own.SerializeToString().hex()))

    unit = pack.units[slave.serial_number]
    assert (unit.pv_power_1, unit.pv_power_2, unit.pv_power_3) == (321, 0, 45)


async def test_pack_unit_without_solar_has_no_solar_input(pack, master):
    assert not hasattr(pack.units[master.serial_number], "pv_power_1")


async def test_pack_sends_the_chosen_interval_again_when_a_unit_lost_it(
    mocker, pack, master
):
    send = mocker.patch.object(master, "set_report_interval", mocker.AsyncMock())
    pack.desired_report_interval = 30

    master.update_state("pack_report_interval", 120.0)
    await asyncio.gather(*pack._tasks)

    send.assert_awaited_once_with(30)


async def test_pack_leaves_a_unit_already_on_the_chosen_interval(mocker, pack, master):
    send = mocker.patch.object(master, "set_report_interval", mocker.AsyncMock())

    master.update_state("pack_report_interval", 30.0)

    assert not pack._tasks
    send.assert_not_awaited()


async def test_pack_unit_shows_what_only_its_own_link_carries(pack, slave):
    await slave.data_parse(_telemetry("e20212083a1017180020902728d71630b22e38b32e"))

    unit = pack.units[slave.serial_number]
    assert unit.battery_full_capacity == 5008
    assert unit.battery_remaining_energy == 2903


async def test_pack_unit_sends_its_ac_ports_over_its_own_link(pack, master, slave):
    await pack.units[slave.serial_number].enable_ac_ports(True)

    slave._conn.send_packet.assert_awaited_once()
    master._conn.send_packet.assert_not_awaited()


async def test_pack_unit_fails_loudly_when_its_link_is_down(pack, slave):
    slave._conn = None

    with pytest.raises(NotConnectedError):
        await pack.units[slave.serial_number].enable_ac_ports(True)


def _limits(feed: int, grid_in: int) -> str:
    message = es22_bkw_pb2.DisplayPropertyUpload()
    message.power_parameter_config.on_grid_feed_power_limit = feed
    message.power_parameter_config.on_grid_take_power_limit = grid_in
    return message.SerializeToString().hex()


def _sent_limits(unit) -> list[tuple[int, int]]:
    sent = []
    for call in unit._conn.send_packet.await_args_list:
        command = es22_bkw_pb2.PropertyCmd.FromString(call.args[0].payload)
        config = command.power_parameter_config
        sent.append((config.on_grid_feed_power_limit, config.on_grid_take_power_limit))
    return sent


@pytest.fixture
async def limited_pack(pack, master):
    await master.data_parse(_telemetry(_limits(900, 3000)))
    return pack


async def test_standby_zeroes_both_limits(limited_pack, master):
    await limited_pack.enable_pack_standby(True)

    assert limited_pack.pack_standby is True
    assert _sent_limits(master) == [(0, 0), (0, 0)]


async def test_leaving_standby_restores_both_limits(limited_pack, master):
    await limited_pack.enable_pack_standby(True)
    await master.data_parse(_telemetry(_limits(0, 0)))
    master._conn.send_packet.reset_mock()

    await limited_pack.enable_pack_standby(False)

    assert limited_pack.pack_standby is False
    assert _sent_limits(master) == [(900, 0), (0, 3000)]


@pytest.mark.parametrize(
    "setter", ["set_feed_grid_pow_limit", "set_grid_in_power_limit"]
)
async def test_standby_refuses_a_new_limit(limited_pack, master, setter):
    await limited_pack.enable_pack_standby(True)
    master._conn.send_packet.reset_mock()

    with pytest.raises(ValueError, match="standby"):
        await getattr(limited_pack, setter)(500)

    master._conn.send_packet.assert_not_awaited()


async def test_standby_ignores_the_reports_sent_before_its_own_write(
    limited_pack, master
):
    await limited_pack.enable_pack_standby(True)
    master._conn.send_packet.reset_mock()

    await master.data_parse(_telemetry(_limits(900, 3000)))

    assert limited_pack.pack_standby is True
    assert not limited_pack._tasks


async def test_an_outside_change_is_kept_and_only_the_other_limit_comes_back(
    limited_pack, master
):
    await limited_pack.enable_pack_standby(True)
    await master.data_parse(_telemetry(_limits(0, 0)))
    master._conn.send_packet.reset_mock()

    await master.data_parse(_telemetry(_limits(500, 0)))
    await asyncio.gather(*limited_pack._tasks)

    assert limited_pack.pack_standby is False
    assert _sent_limits(master) == [(0, 3000)]


async def test_a_change_of_both_limits_ends_standby_without_writing(
    limited_pack, master
):
    await limited_pack.enable_pack_standby(True)
    await master.data_parse(_telemetry(_limits(0, 0)))
    master._conn.send_packet.reset_mock()

    await master.data_parse(_telemetry(_limits(500, 2000)))
    await asyncio.gather(*limited_pack._tasks)

    assert limited_pack.pack_standby is False
    master._conn.send_packet.assert_not_awaited()


async def test_standby_puts_the_limits_back_when_it_can_not_finish(
    limited_pack, master, slave
):
    gone = NotConnectedError("gone")
    master._conn.send_packet.side_effect = [None, gone, None, None]
    slave._conn.send_packet.side_effect = [gone]

    with pytest.raises(NotConnectedError):
        await limited_pack.enable_pack_standby(True)

    assert limited_pack.pack_standby is False
    assert _sent_limits(master)[-2:] == [(900, 0), (0, 3000)]


async def test_standby_keeps_the_first_error_when_the_limits_can_not_go_back(
    limited_pack, master, slave
):
    master._conn.send_packet.side_effect = NotConnectedError("gone")
    slave._conn.send_packet.side_effect = NotConnectedError("gone")

    with pytest.raises(NotConnectedError):
        await limited_pack.enable_pack_standby(True)

    assert limited_pack.pack_standby is False


async def test_standby_survives_the_first_report_after_a_restart(pack, master):
    # right after a restart neither limit is known, and they arrive one after the other
    pack.restore_standby(StandbyLimits(900, 3000))

    await master.data_parse(_telemetry(_limits(0, 0)))

    assert pack.pack_standby is True
    assert not pack._tasks
    master._conn.send_packet.assert_not_awaited()


async def test_standby_survives_a_restart(limited_pack, master):
    limited_pack.restore_standby(StandbyLimits(900, 3000))

    await master.data_parse(_telemetry(_limits(0, 0)))
    master._conn.send_packet.reset_mock()
    await limited_pack.enable_pack_standby(False)

    assert _sent_limits(master) == [(900, 0), (0, 3000)]


async def test_pack_reads_and_sends_its_work_mode(pack, master, slave):
    await slave.data_parse(_telemetry("c80102"))
    assert pack.work_mode is WorkMode.TIMER_TASK

    await pack.set_work_mode(WorkMode.AI_MODE)

    sent = master._conn.send_packet.await_args.args[0]
    command = es22_bkw_pb2.PropertyCmd.FromString(sent.payload)
    assert command.action_id == 25
    assert command.work_mode == WorkMode.AI_MODE.value


async def test_pack_shows_an_accepted_setting_before_the_next_report(limited_pack):
    await limited_pack.set_feed_grid_pow_limit(600)

    assert limited_pack.feed_grid_pow_limit == 600


async def test_pack_keeps_the_old_value_of_a_setting_nobody_accepted(
    limited_pack, master, slave
):
    master._conn.send_packet.side_effect = NotConnectedError("gone")
    slave._conn.send_packet.side_effect = NotConnectedError("gone")

    with pytest.raises(NotConnectedError):
        await limited_pack.set_feed_grid_pow_limit(600)

    assert limited_pack.feed_grid_pow_limit == 900


async def test_standby_shows_its_zeros_at_once(limited_pack):
    await limited_pack.enable_pack_standby(True)

    assert limited_pack.feed_grid_pow_limit == 0
    assert limited_pack.grid_in_power_limit == 0


async def test_pack_command_fails_loudly_when_no_unit_answers(pack, master, slave):
    master._conn.send_packet.side_effect = NotConnectedError("gone")
    slave._conn.send_packet.side_effect = NotConnectedError("gone")

    with pytest.raises(NotConnectedError):
        await pack.set_energy_backup_battery_level(60)


async def test_pack_stops_listening_to_a_unit_that_left(pack, slave, master):
    pack.detach(slave)
    pack.detach(master)
    await slave.data_parse(_telemetry(TOTALS))

    assert pack.battery_level is None
    assert not pack.is_connected
