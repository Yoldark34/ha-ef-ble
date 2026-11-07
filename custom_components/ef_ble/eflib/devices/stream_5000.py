from . import stream_ac_5000


class Device(stream_ac_5000.Device):
    """STREAM 5000"""

    SN_PREFIX = (b"ES21",)
