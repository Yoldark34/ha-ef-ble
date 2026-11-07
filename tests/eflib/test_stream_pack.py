import pytest
from bleak.exc import BleakError
from pytest_mock import MockerFixture

from custom_components.ef_ble.eflib.devices import stream_5000, stream_ac_5000
from custom_components.ef_ble.eflib.exceptions import NotConnectedError
from custom_components.ef_ble.eflib.packet import Packet
from custom_components.ef_ble.eflib.pack import Pack

MASTER = "da01021001"
SLAVE = "da01021002"
# A captured post of the pack's totals, with the house at 887 W and the battery idle
TOTALS = "5a0808ee0d10ee0d283a620330f706"


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
