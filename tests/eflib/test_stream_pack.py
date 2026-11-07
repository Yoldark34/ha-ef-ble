import pytest
from bleak.exc import BleakError
from pytest_mock import MockerFixture

from custom_components.ef_ble.eflib.devices import stream_5000, stream_ac_5000
from custom_components.ef_ble.eflib.exceptions import NotConnectedError
from custom_components.ef_ble.eflib.pack import Pack, PackStatus
from custom_components.ef_ble.eflib.packet import Packet

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
