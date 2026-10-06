"""From the API's data points to the closed value-type vocabulary, and from its
units to Brick's (QUDT), with the same tables as BACnet/IP: the objects are
BACnet objects. See ``README.md``, "Value types and
units".
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bacpypes3.basetypes import EngineeringUnits
from bricklogger.sdk.bacnet import (
    ANALOG_TYPES,
    BINARY_TYPES,
    DATETIME_TYPES,
    INTEGER_TYPES,
    MULTISTATE_TYPES,
    QUDT,
    STRING_TYPES,
    UNIT_MAP,
    protocol_unit,
)
from bricklogger.sdk.contract import NullReason, ValueType

TRUE_TEXTS = frozenset({"active", "true", "on", "1"})
FALSE_TEXTS = frozenset({"inactive", "false", "off", "0"})
NO_UNITS = "no-units"

UNIT_SYMBOLS: Mapping[str, str] = {
    "%": "PERCENT",
    "%RH": "PERCENT_RH",
    "°C": "DEG_C",
    "°F": "DEG_F",
    "K": "K",
    "Pa": "PA",
    "hPa": "HectoPA",
    "kPa": "KiloPA",
    "bar": "BAR",
    "mbar": "MilliBAR",
    "L/s": "L-PER-SEC",
    "l/s": "L-PER-SEC",
    "L/min": "L-PER-MIN",
    "l/min": "L-PER-MIN",
    "L/h": "L-PER-HR",
    "l/h": "L-PER-HR",
    "m³/s": "M3-PER-SEC",
    "m3/s": "M3-PER-SEC",
    "m³/h": "M3-PER-HR",
    "m3/h": "M3-PER-HR",
    "cfm": "FT3-PER-MIN",
    "m³": "M3",
    "m3": "M3",
    "L": "L",
    "l": "L",
    "W": "W",
    "kW": "KiloW",
    "MW": "MegaW",
    "Wh": "W-HR",
    "kWh": "KiloW-HR",
    "MWh": "MegaW-HR",
    "J": "J",
    "kJ": "KiloJ",
    "MJ": "MegaJ",
    "GJ": "GigaJ",
    "V": "V",
    "mV": "MilliV",
    "A": "A",
    "mA": "MilliA",
    "Hz": "HZ",
    "ppm": "PPM",
    "ppb": "PPB",
    "lux": "LUX",
    "lx": "LUX",
    "m/s": "M-PER-SEC",
    "km/h": "KiloM-PER-HR",
    "m": "M",
    "mm": "MilliM",
    "cm": "CentiM",
    "m²": "M2",
    "m2": "M2",
    "kg": "KiloGM",
    "g": "GM",
    "kg/h": "KiloGM-PER-HR",
    "s": "SEC",
    "min": "MIN",
    "h": "HR",
    "d": "DAY",
    "°": "DEG",
    "rpm": "REV-PER-MIN",
    "W/m²": "W-PER-M2",
    "W/m2": "W-PER-M2",
    "mg/m³": "MilliGM-PER-M3",
    "mg/m3": "MilliGM-PER-M3",
    "µg/m³": "MicroGM-PER-M3",
    "μg/m³": "MicroGM-PER-M3",
    "ug/m3": "MicroGM-PER-M3",
}
"""The unit symbols the API writes in ``units``, to QUDT's local names."""

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[\s_]+")
_HYPHENS = re.compile(r"-+")


def normalise_type(text: Any) -> str | None:
    """``analog-input`` from the spellings an API may use: hyphens, underscores,
    spaces or camel case."""
    if not isinstance(text, str) or not text.strip():
        return None
    hyphenated = _SEPARATORS.sub("-", _CAMEL.sub("-", text.strip()))
    return _HYPHENS.sub("-", hyphenated).lower()


def value_type_for(object_type: str | None) -> ValueType | None:
    """The vocabulary's type for a BACnet object type; ``None`` when there is none."""
    if object_type in ANALOG_TYPES:
        return "number"
    if object_type in INTEGER_TYPES:
        return "integer"
    if object_type in BINARY_TYPES:
        return "boolean"
    if object_type in MULTISTATE_TYPES:
        return "enum"
    if object_type in STRING_TYPES:
        return "string"
    if object_type in DATETIME_TYPES:
        return "datetime"
    return None


def parse_timestamp(text: Any) -> datetime | None:
    """An ISO 8601 timestamp as an aware UTC datetime; a naive one is taken as UTC."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        stamp = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


@dataclass(frozen=True)
class Sample:
    """One data point of a trending answer: the cloud's timestamp, value and text."""

    timestamp: datetime
    value: Any
    text: Any


def samples_of(data: Mapping[str, Any]) -> list[Sample]:
    """The data points of a trending answer, sorted by time; unreadable ones skipped."""
    items = data.get("data_points")
    samples: list[Sample] = []
    if not isinstance(items, list):
        return samples
    for item in items:
        if not isinstance(item, dict):
            continue
        stamp = parse_timestamp(item.get("timestamp"))
        if stamp is None:
            continue
        samples.append(Sample(stamp, item.get("value"), item.get("value_text")))
    samples.sort(key=lambda sample: sample.timestamp)
    return samples


def newest_first(data: Mapping[str, Any]) -> bool | None:
    """Whether a trending answer lists the newest sample first, as the API does;
    ``None`` when the answer has fewer than two readable timestamps."""
    items = data.get("data_points")
    if not isinstance(items, list):
        return None
    stamps = [
        parse_timestamp(item.get("timestamp"))
        for item in items
        if isinstance(item, dict)
    ]
    readable = [stamp for stamp in stamps if stamp is not None]
    if len(readable) < 2 or readable[0] == readable[-1]:
        return None
    return readable[0] > readable[-1]


@dataclass(frozen=True)
class Converted:
    """One data point as the vocabulary sees it, and the text it teaches."""

    type: ValueType
    value: Any = None
    reason: NullReason | None = None
    text: tuple[int, str] | None = None


NO_VALUE = Converted("null", None, "no_value")


def convert(value_type: ValueType, value: Any, value_text: Any) -> Converted:
    """The API's ``value`` and ``value_text`` as an observation of the point's type.

    The type says which of the two carries the value; a sample with neither is
    ``null`` with ``no_value``. For ``enum`` and ``boolean`` a text beside the
    state that is not just the number itself is the text the point learns for
    it; the API as observed repeats the number, so nothing is learned from it.
    """
    text = value_text.strip() if isinstance(value_text, str) else ""
    number = _number(value)
    if value_type in ("number", "integer"):
        if number is None and text:
            number = _number(text)
        if number is None:
            return NO_VALUE
        if value_type == "integer":
            return Converted("integer", int(number))
        return Converted("number", number)
    if value_type == "boolean":
        flag: bool | None = None
        if number is not None:
            flag = number != 0
        elif text.lower() in TRUE_TEXTS:
            flag = True
        elif text.lower() in FALSE_TEXTS:
            flag = False
        if flag is None:
            return NO_VALUE
        return Converted("boolean", flag, text=_label(int(flag), text))
    if value_type == "enum":
        if number is None and text:
            number = _number(text)
        if number is None:
            return NO_VALUE
        ordinal = int(number)
        return Converted("enum", ordinal, text=_label(ordinal, text))
    if value_type == "string":
        if text:
            return Converted("string", text)
        if number is not None:
            return Converted("string", str(value))
        return NO_VALUE
    if value_type == "datetime":
        stamp = parse_timestamp(text)
        if stamp is None:
            return NO_VALUE
        return Converted("datetime", stamp)
    return NO_VALUE


def _label(ordinal: int, text: str) -> tuple[int, str] | None:
    """The text a state teaches: one that is not just the number itself."""
    if not text or _number(text) is not None:
        return None
    return (ordinal, text)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def unit_for(unit_id: Any, units: Any) -> str | None:
    """QUDT where BACnet's unit number or the API's unit symbol has a
    counterpart, otherwise the API's own text; nothing for ``no-units``."""
    if isinstance(unit_id, int) and not isinstance(unit_id, bool):
        try:
            name = str(EngineeringUnits(unit_id))
        except Exception:
            name = ""
        if name:
            return protocol_unit(name)
    if isinstance(units, str) and units.strip():
        symbol = units.strip()
        if symbol in UNIT_SYMBOLS:
            return QUDT + UNIT_SYMBOLS[symbol]
        normalised = normalise_type(symbol)
        if normalised == NO_UNITS:
            return None
        if normalised in UNIT_MAP:
            return protocol_unit(normalised)
        return symbol
    return None
