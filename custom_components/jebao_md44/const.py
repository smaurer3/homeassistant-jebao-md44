"""Constants for the Jebao MD-4.4 integration (local control only)."""
from typing import Final

DOMAIN: Final = "jebao_md44"

# Configuration keys
CONF_HOST: Final = "host"
CONF_DEVICE_ID: Final = "device_id"
CONF_MODEL: Final = "model"
CONF_DID: Final = "did"
CONF_MAC: Final = "mac_address"

# Defaults
DEFAULT_NAME: Final = "Jebao MD-4.4 Doser"
# The pump pushes a status report whenever anything changes, so polling is
# only a safety net for a missed push or a silently dropped socket.
DEFAULT_SCAN_INTERVAL: Final = 300  # seconds

MODEL_MD44: Final = "MD-4.4"

# Network
TCP_PORT: Final = 12416
UDP_DISCOVERY_PORT: Final = 12414

# Product keys whose P0 layout matches ``lan_proto.LAYOUT`` (792-byte buffer,
# 39 writable datapoints, 5 attr_flag bytes). Verified live against
# 1aa33c38 (4-head WiFi+BLE, ESP32-C3, firmware 04X3000R). 3b181431 declares
# an identical datapoint set in the vendor app's product list; it is accepted
# but the layout is confirmed at runtime from the reported P0 length.
SUPPORTED_PRODUCT_KEYS: Final = {
    "1aa33c38ba9d4b78a9e7796705b2fad7",
    "3b181431429e46029f850348869cc66f",
}

# The MD-4.4's firmware exposes channe1..channe8 / Timer1..8ON /
# IntervalT1..8 — likely because it is shared with an 8-head SKU — but the
# physical pump body has only 4 outputs. We only surface the four that do
# anything; channels 5..8 read as always-False and writes to them are
# ignored by the pump.
MD44_CHANNEL_COUNT: Final = 4

# Calibration-factor mode for sub-mL dosing precision.
#
# The official app's UI only accepts whole-mL dose values. Users who need
# finer dosing physically calibrate the pump so it dispenses 1/10th of the
# value it thinks it is dispensing — i.e. tell the pump during calibration
# that it just dispensed 300 mL when it only dispensed 30. After that,
# every entry of "10" in the app actually doses 1 mL, "14" doses 1.4 mL.
#
# This integration mirrors that trick with a per-config-entry toggle: when
# enabled, the Channel N schedule text entity shows fractional mL values
# (e.g. 12:00=1.4) and we multiply by 10 before writing to the pump.
OPT_CAL_FACTOR_10X: Final = "cal_factor_10x"
CAL_FACTOR_ON: Final = 10
CAL_FACTOR_OFF: Final = 1


def cal_factor(entry_options: dict) -> int:
    """Return the active calibration multiplier for an entry (1 or 10)."""
    return CAL_FACTOR_ON if entry_options.get(OPT_CAL_FACTOR_10X) else CAL_FACTOR_OFF


def signal_dose_input_changed(entry_id: str) -> str:
    """Dispatcher signal fired when this entry's Calibration amount value
    changes. Subscribed by the Value-to-enter-in-app sensor so it can
    re-render immediately rather than waiting for the next update."""
    return f"jebao_md44_{entry_id}_dose_input_changed"


def signal_cal_factor_changed(entry_id: str) -> str:
    """Dispatcher signal fired when the 10x precision toggle flips for this
    entry. Subscribed by every schedule text entity (so they switch between
    integer / decimal display) and by the Value-to-enter-in-app sensor."""
    return f"jebao_md44_{entry_id}_cal_factor_changed"
