import dataclasses

import pytest
from pytest_mock import MockerFixture

from custom_components.ef_ble.eflib.devices import stream_5000
from custom_components.ef_ble.eflib.devices.stream_ac_5000 import Device, SystemRole
from custom_components.ef_ble.eflib.packet import Packet, PacketV4
from custom_components.ef_ble.eflib.pb import es22_bkw_pb2
from custom_components.ef_ble.eflib.serial_routing import SerialRouting


@pytest.fixture
def payload_sequence():
    """Telemetry payloads captured from a STREAM AC 5000, with the serial masked"""
    return [
        # discharging into a 300 W load: battery, converter and backup port power
        "5a0b08d80420d704281430d804620320ac02720628d70438d804e2020710b50528ca950492032a0a280a1045533232585858585858585858343437150000a04125822896c328023520b79b433d82289643",
        # battery SoC, cell temperatures and the charge limits
        "720628d70438d804b2023a0a380813106418bd0528a02730f9950438c40140b32e48c91950ca1958236027682470247a01008a011045533232585858585858585858343437b2031b0a190a10455332325858585858585858583434371014180020d704",
        # charging with a 200 W input limit, which pins down the sign of battery power
        "5a0e08900310a406209403280d309303620630c80138ca0192032a0a280a10455332325858585858585858583434371500005041257fc04943280235d82941c33d7fc049c3b2031b0a190a1045533232585858585858585858343437100d1800209203",
        # the configuration blocks, which ride along with the telemetry; the unit's
        # group and LAN identifiers are blanked
        "2a020807521008c41310c4131801200428a00630c4137a00820100920104080012009a01020801ba010710900328003000c80102d201100a0c0800080008000800080008001200da010408002001ea010408641000f2010408001028fa010408c0ee6d820206080010001800ea020228008203020800c203020800",
    ]


@pytest.fixture
def routed_frame():
    """A serial-routed V4 frame from the same capture, serial masked"""
    # unobfuscated, and closing with a sentinel where a session frame keeps its CRC16
    return "aa04a001d7010001214553323258585858585858585834343721014003032efe27de020121015a0e08900310e41220d40f280230d30f620630c80138ea077214080010001800200028d40f300238d30f40004801a201020800ba01060887a8e5d306da0104100118018a023c0d00001d4515000000001d72f97f3f25a4707d3f2d00809c453509eb1b403d0000c84245000000004d0000484455000048445d00401c456500b01a459202009a02020800b2023a0a3808021064189e0e28902730d0830138b32e40f00248f61850fc1858256028682a702a7a01008a011045533232585858585858585858343437c202310a2f0802100118012001280130003a04e882e021421b08011a170a1045533232585858585858585858343437103218e807d80202e20214080210a20e180020902728c7830130b32e38f002f00200f8020082030a08001000180020002a0092032a0a280a10455332325858585858585858583434371500000040256bb27a442802354d6e79c43d6bb27ac4b2031b0a190a10455332325858585858585858583434371002180020d40fca030c0a0a00000000000000000000bbbb"


@pytest.fixture
def idle_payload_sequence():
    """Payloads from a firmware 1.1.6.18 unit, its battery idle and the house on grid"""
    return [
        # the sub-device energy flow block, posted empty
        "920300",
        # a watt on the battery and on the grid port, with the battery block empty
        "5a0c08cc0e10ce0e2002283a3001620530a6073801b20300",
        # both of those down to zero, and left out
        "5a0808ee0d10ee0d283a620330f706b20300",
    ]


@pytest.fixture
def device(mocker: MockerFixture):
    ble_dev = mocker.Mock()
    ble_dev.address = "AA:BB:CC:DD:EE:FF"
    adv_data = mocker.MagicMock()
    device = Device(ble_dev, adv_data, "ES22XXXXXXXXX447")
    device._conn = mocker.AsyncMock()
    return device


def _telemetry(payload_hex: str) -> Packet:
    return Packet(0x02, 0x21, 0xFE, 0x27, bytes.fromhex(payload_hex), 0x01, 0x01, 0x03)


async def test_stream_ac_5000_processes_all_payloads_successfully(
    device, payload_sequence
):
    for i, payload_hex in enumerate(payload_sequence):
        processed = await device.data_parse(_telemetry(payload_hex))
        assert processed is True, f"Payload {i} was not processed"


async def test_stream_ac_5000_parses_battery_level_and_temperature(
    device, payload_sequence
):
    processed = await device.data_parse(_telemetry(payload_sequence[1]))

    assert processed is True
    assert device.battery_level_main == 19
    # the pack reports four temperatures; the hottest one is what gets exposed
    assert device.cell_temperature == 39


async def test_stream_ac_5000_reports_battery_power_negative_while_discharging(
    device, payload_sequence
):
    processed = await device.data_parse(_telemetry(payload_sequence[0]))

    assert processed is True
    assert device.unit_battery_power == -300
    assert device.unit_battery_ac_input_power == 0
    assert device.unit_battery_ac_output_power == 300
    assert device.pack_load_from_battery == 300
    assert device.backup_port_power == 0


async def test_stream_ac_5000_reports_battery_power_positive_while_charging(
    device, payload_sequence
):
    processed = await device.data_parse(_telemetry(payload_sequence[2]))

    assert processed is True
    assert device.pack_battery_power == 202
    assert device.pack_battery_level == 13


async def test_stream_ac_5000_ignores_packets_from_other_modules(device):
    processed = await device.data_parse(
        Packet(0x35, 0x21, 0xFE, 0x27, b"", 0x01, 0x01, 0x03)
    )

    assert processed is False


async def test_stream_ac_5000_parses_serial_routed_v4_frames(device, routed_frame):
    packet = await device.packet_parse(bytes.fromhex(routed_frame))

    assert isinstance(packet, PacketV4)
    assert packet.sentinel
    assert not packet.obfuscated

    envelope = SerialRouting.envelope(packet.payload)
    assert envelope is not None
    assert (envelope.src, envelope.cmd_set, envelope.cmd_id) == (0x02, 0xFE, 0x27)

    assert await device.data_parse(packet) is True


async def test_stream_ac_5000_reads_its_configuration_blocks(device, payload_sequence):
    packet = _telemetry(payload_sequence[3])

    assert await device.data_parse(packet) is True

    assert device.pack_battery_charge_limit_max == 100
    assert device.pack_battery_charge_limit_min == 0
    assert device.pack_energy_backup is False
    assert device.pack_energy_backup_battery_level == 40
    assert device.ac_ports is True


async def test_stream_ac_5000_routed_frame_round_trips(routed_frame):
    original = bytes.fromhex(routed_frame)

    packet = Packet.from_bytes(original)

    assert isinstance(packet, PacketV4)
    assert packet.to_bytes() == original


async def test_stream_ac_5000_reads_the_stream_report_when_sub_dev_blocks_are_empty(
    device, idle_payload_sequence
):
    for payload_hex in idle_payload_sequence[:2]:
        await device.data_parse(_telemetry(payload_hex))

    assert device.pack_battery_level == 58
    assert device.pack_battery_power == 1
    assert device.pack_grid_power == -1
    assert device.pack_house_grid_power == 935
    assert device.pack_load_from_grid == 934


async def test_stream_ac_5000_zeroes_readings_a_posted_block_leaves_out(
    device, idle_payload_sequence
):
    for payload_hex in idle_payload_sequence:
        await device.data_parse(_telemetry(payload_hex))

    assert device.pack_battery_power == 0
    assert device.pack_grid_power == 0
    assert device.pack_load_from_battery == 0
    assert device.pack_load_from_grid == 887


@pytest.mark.parametrize(
    ("v4_type_a", "v4_type_b", "cmd_flags"),
    [
        pytest.param(0x00, 0x01, 0x11, id="clear_frame"),
        pytest.param(0x13, 0x01, 0x10, id="session_frame"),
    ],
)
async def test_stream_ac_5000_write_carries_the_session_layer_of_the_post(
    device, routed_frame, v4_type_a, v4_type_b, cmd_flags
):
    post = await device.packet_parse(bytes.fromhex(routed_frame))
    await device.data_parse(
        dataclasses.replace(post, v4_type_a=v4_type_a, v4_type_b=v4_type_b)
    )

    await device.set_energy_backup_battery_level(60)

    assert device._conn.send_packet.await_args.args[0].cmd_flags == cmd_flags


@pytest.mark.parametrize(
    ("device_class", "serial", "claimed"),
    [
        (stream_5000.Device, b"ES21XXXXXXXXX180", True),
        (stream_5000.Device, b"ES22XXXXXXXXX447", False),
        (Device, b"ES21XXXXXXXXX180", False),
        (Device, b"ES22XXXXXXXXX447", True),
    ],
)
def test_stream_5000_models_each_claim_only_their_own_serial(
    device_class, serial, claimed
):
    assert device_class.check(serial) is claimed


async def test_stream_5000_reads_the_same_telemetry(mocker, idle_payload_sequence):
    ble_dev = mocker.Mock()
    ble_dev.address = "AA:BB:CC:DD:EE:FF"
    device = stream_5000.Device(ble_dev, mocker.MagicMock(), "ES21XXXXXXXXX180")

    await device.data_parse(_telemetry(idle_payload_sequence[2]))

    assert device.device == "STREAM 5000"
    assert device.pack_battery_level == 58
    assert device.pack_house_grid_power == 887


async def test_stream_ac_5000_reads_its_role_in_the_pack(device):
    # role and unit count, as they arrive in two separate posts
    await device.data_parse(_telemetry("da010410021801"))
    await device.data_parse(_telemetry("da01022002"))

    assert device.system_role == SystemRole.SLAVE
    assert device.pack_unit_count == 2


async def test_stream_5000_reports_each_solar_input(mocker):
    ble_dev = mocker.Mock()
    ble_dev.address = "AA:BB:CC:DD:EE:FF"
    device = stream_5000.Device(ble_dev, mocker.MagicMock(), "ES21XXXXXXXXX180")

    # every capture so far is without sun, so the reading has to be built
    message = es22_bkw_pb2.DisplayPropertyUpload()
    message.dev_energy_stream_report.pv1_pwr = 321
    message.dev_energy_stream_report.pv3_pwr = 45
    device.update_from_message(message)

    assert [device.pv_power[n] for n in range(1, 5)] == [321, 0, 45, 0]


async def test_stream_ac_5000_reports_battery_capacity(device):
    # a unit at 58 %, which is what 2903 Wh of 5008 Wh comes to
    await device.data_parse(_telemetry("e20212083a1017180020902728d71630b22e38b32e"))

    assert device.battery_full_capacity == 5008
    assert device.battery_remaining_energy == 2903


async def test_stream_ac_5000_exact_values_from_known_packets(
    device, payload_sequence, routed_frame
):
    for payload_hex in payload_sequence:
        await device.data_parse(_telemetry(payload_hex))
    await device.data_parse(await device.packet_parse(bytes.fromhex(routed_frame)))

    expected = {
        Device.pack_battery_level: 2,
        Device.battery_level_main: 2,
        Device.unit_battery_power: 1002,
        Device.unit_battery_ac_input_power: 1002,
        Device.unit_battery_ac_output_power: 0,
        Device.unit_grid_power: -1002,
        Device.pack_house_grid_power: 1202,
        Device.backup_port_power: 0,
        Device.pack_load_system: 200,
        Device.pack_load_from_battery: 0,
        Device.pack_load_from_grid: 200,
        Device.cell_temperature: 40,
        Device.remaining_time_charging: 368,
        Device.remaining_time_discharging: 5939,
        Device.pack_battery_charge_limit_max: 100,
        Device.pack_battery_charge_limit_min: 0,
        Device.pack_energy_backup: False,
        Device.pack_energy_backup_battery_level: 40,
        Device.ac_ports: True,
    }

    for field, expected_value in expected.items():
        actual_value = device.get_value(field)
        assert actual_value == expected_value, (
            f"{field.public_name}: expected {expected_value}, got {actual_value}"
        )
