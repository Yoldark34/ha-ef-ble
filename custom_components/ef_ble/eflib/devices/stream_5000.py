from ..props import pb_field_group
from . import stream_ac_5000

pb = stream_ac_5000.pb


class Device(stream_ac_5000.Device):
    """STREAM 5000"""

    SN_PREFIX = (b"ES21",)

    pv_power = pb_field_group(
        pb.dev_energy_stream_report.pv1_pwr,
        match="pv{n}_pwr",
        count=4,
        transform=stream_ac_5000.ZERO_IF_OMITTED,
        name_template="pv_power_{n}",
    )
