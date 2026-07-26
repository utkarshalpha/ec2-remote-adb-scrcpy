"""Pure state model for the Convrse AI display guard.

This module deliberately contains no ADB, subprocess, or UI code.  It models the
Rockchip firmware's Display Settings switches and answers two questions:

* what state would the vendor Settings screen show for each switch; and
* which properties must be corrected before the device is verified off.

The preference/property mappings and unusual ``-1`` on-values mirror
``com.android.tv.settings.display.DisplayFragment`` on the audited RK3576
firmware.  A value is verified off only when it is exactly the declared
``off_value``.  Unknown and missing values are drift, even if the Settings UI
would not render them as checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping
from xml.etree import ElementTree
from xml.parsers import expat


PropertyValue = str | int | bool | bytes | None
AUTO_ENFORCE_ON_CONNECT = True


@dataclass(frozen=True, slots=True)
class ToggleSpec:
    """Authoritative description of one vendor Display Settings switch."""

    name: str
    preference_key: str
    property_key: str
    on_values: tuple[str, ...]
    off_value: str = "0"


# Keep this order aligned with the switch order shown by the firmware UI.
AI_DISPLAY_TOGGLES: tuple[ToggleSpec, ...] = (
    ToggleSpec(
        name="AI VisionPQ",
        preference_key="ai_visionpq",
        property_key="persist.vendor.sculptor.mode",
        on_values=("1",),
    ),
    ToggleSpec(
        name="AI-DC",
        preference_key="ai_dc",
        property_key="persist.vendor.rkpq.dc.enable",
        on_values=("-1",),
    ),
    ToggleSpec(
        name="AI-SD",
        preference_key="ai_pq",
        property_key="persist.vendor.rkpq.hwpq_aisd_enable",
        on_values=("1",),
    ),
    ToggleSpec(
        name="AI-SR",
        preference_key="ai_sr",
        property_key="persist.vendor.rkpq.sr.enable",
        on_values=("-1",),
    ),
    ToggleSpec(
        name="AI-MEMC",
        preference_key="ai_memc",
        property_key="persist.vendor.rkpq.memc.enable",
        on_values=("-1",),
    ),
    ToggleSpec(
        name="AI-FACE",
        preference_key="ai_face",
        property_key="persist.vendor.rkpq.fe.enable",
        on_values=("1",),
    ),
    ToggleSpec(
        name="AI Test Mode",
        preference_key="ai_demonstration_switch",
        property_key="persist.vendor.rkpq.memc.watermark",
        on_values=("1",),
    ),
)


# Secondary gates written by the vendor's master VisionPQ path, plus the V1
# guard's non-toggle properties.  ``persist.vendor.sculptor.enable.fe`` is
# intentionally absent: the firmware writes that capability gate to 1 even
# when AI-FACE is switched off.
EXTRA_OFF_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("persist.vendor.sculptor.c2.mode", "0"),
    ("persist.vendor.rkpq.hwpq_lce_ratio", "0"),
    ("persist.vendor.rkpq.hwpq_shp_en", "0"),
    ("persist.vendor.rkpq.iptv_sr_enable", "0"),
    ("persist.vendor.rkpq.memc.stp.enable", "0"),
    ("vendor.hwc.hwpq_force_enable", "0"),
    ("vendor.tvinput.rkpq.vdpp_shp_en", "0"),
)


PRESERVED_FIRMWARE_PROPERTIES: tuple[str, ...] = (
    "persist.vendor.sculptor.enable.fe",
)


class ToggleState(str, Enum):
    """State the vendor Settings UI would derive from one raw property."""

    OFF = "off"
    ON = "on"
    UNKNOWN = "unknown"
    MISSING = "missing"


class CapabilityLevel(str, Enum):
    """How much of the audited AI display interface a device exposes."""

    UNSUPPORTED = "unsupported"
    PARTIAL = "partial"
    FULL = "full"


class SkipReason(str, Enum):
    """Why a known guard property must not be applied to a device."""

    UNSUPPORTED_OR_MISSING = "unsupported_or_missing"
    INCOMPATIBLE_FIRMWARE = "incompatible_firmware"


@dataclass(frozen=True, slots=True)
class ToggleEvaluation:
    """Evaluated UI state and guard drift for one display switch."""

    spec: ToggleSpec
    raw_value: str | None
    state: ToggleState
    drift: bool

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def preference_key(self) -> str:
        return self.spec.preference_key

    @property
    def property_key(self) -> str:
        return self.spec.property_key

    @property
    def expected_value(self) -> str:
        return self.spec.off_value


@dataclass(frozen=True, slots=True)
class PropertyEvaluation:
    """Verification result for a non-toggle guard property."""

    property_key: str
    raw_value: str | None
    expected_value: str
    drift: bool


@dataclass(frozen=True, slots=True)
class StaleOnToggle:
    """A cached Settings preference that is on while runtime is verified off."""

    spec: ToggleSpec
    preference_value: bool
    runtime_value: str

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def preference_key(self) -> str:
        return self.spec.preference_key

    @property
    def property_key(self) -> str:
        return self.spec.property_key


@dataclass(frozen=True, slots=True)
class SharedPreferencesSyncResult:
    """Result of a validated, content-preserving preference cache rewrite."""

    xml_data: str | bytes
    changed: bool
    changed_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkippedProperty:
    """A known property deliberately excluded from a device application plan."""

    property_key: str
    expected_value: str
    reason: SkipReason
    toggle_name: str | None = None


@dataclass(frozen=True, slots=True)
class GuardApplicationPlan:
    """Pure, capability-aware plan for one opaque connected-device identifier.

    ``device_id`` is intentionally not parsed as an IP address or constrained to
    a transport. It may be an ADB serial, USB identifier, hostname, or network
    endpoint supplied by the connection layer.
    """

    device_id: str
    automatic: bool
    capability: CapabilityLevel
    supported_properties: tuple[PropertyEvaluation, ...]
    skipped_properties: tuple[SkippedProperty, ...]

    @property
    def compatible(self) -> bool:
        return self.capability is not CapabilityLevel.UNSUPPORTED

    @property
    def supported_toggle_specs(self) -> tuple[ToggleSpec, ...]:
        supported_keys = {
            item.property_key for item in self.supported_properties
        }
        return tuple(
            spec
            for spec in AI_DISPLAY_TOGGLES
            if spec.property_key in supported_keys
        )

    @property
    def skipped_toggle_specs(self) -> tuple[ToggleSpec, ...]:
        supported_keys = {
            spec.property_key for spec in self.supported_toggle_specs
        }
        return tuple(
            spec
            for spec in AI_DISPLAY_TOGGLES
            if spec.property_key not in supported_keys
        )

    @property
    def enforces_all_toggles(self) -> bool:
        return (
            self.automatic
            and self.capability is CapabilityLevel.FULL
            and len(self.supported_toggle_specs) == len(AI_DISPLAY_TOGGLES)
        )

    @property
    def application_map(self) -> dict[str, str]:
        """Return every supported value to enforce for this connection.

        Supported properties are included even when already off so a full RK
        profile applies and verifies all seven visible controls on every new
        connection. Unsupported properties can never appear in this map.
        """

        if not self.automatic or not self.compatible:
            return {}
        return {
            item.property_key: item.expected_value
            for item in self.supported_properties
        }

    @property
    def correction_map(self) -> dict[str, str]:
        """Return supported values that currently differ from required off."""

        if not self.automatic or not self.compatible:
            return {}
        return {
            item.property_key: item.expected_value
            for item in self.supported_properties
            if item.drift
        }

    @property
    def should_apply(self) -> bool:
        return bool(self.application_map)


@dataclass(frozen=True, slots=True)
class GuardEvaluation:
    """Complete, immutable evaluation of one getprop snapshot."""

    toggles: tuple[ToggleEvaluation, ...]
    extra_properties: tuple[PropertyEvaluation, ...]

    @property
    def verified_off(self) -> bool:
        return not any(item.drift for item in self.toggles) and not any(
            item.drift for item in self.extra_properties
        )

    @property
    def drifted_properties(self) -> tuple[str, ...]:
        return tuple(
            item.property_key
            for item in (*self.toggles, *self.extra_properties)
            if item.drift
        )

    @property
    def drift_count(self) -> int:
        return len(self.drifted_properties)

    def toggle_for_preference(self, preference_key: str) -> ToggleEvaluation:
        """Return one toggle result or raise ``KeyError`` for an unknown key."""

        for item in self.toggles:
            if item.preference_key == preference_key:
                return item
        raise KeyError(preference_key)

    def toggle_for_property(self, property_key: str) -> ToggleEvaluation:
        """Return one toggle result or raise ``KeyError`` for an unknown key."""

        for item in self.toggles:
            if item.property_key == property_key:
                return item
        raise KeyError(property_key)


def normalize_property_value(value: PropertyValue) -> str | None:
    """Normalize a raw mapping value without inventing a missing value.

    Direct ``getprop <key>`` output is normally a string.  Supporting integers,
    booleans, and bytes keeps the pure evaluator convenient for adapters and
    tests.  Empty/whitespace output is treated as missing and therefore cannot
    be reported as verified off.
    """

    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, bool):
        value = "1" if value else "0"
    normalized = str(value).strip()
    return normalized or None


def parse_shared_preferences_booleans(
    xml_data: str | bytes,
) -> dict[str, bool]:
    """Parse the seven AI toggle booleans from Android SharedPreferences XML.

    Unrelated preferences and non-boolean entries are ignored.  The returned
    mapping follows :data:`AI_DISPLAY_TOGGLES` order regardless of XML order,
    which keeps diagnostics aligned with the visible Settings screen.  Invalid
    or duplicate values for one of the seven authoritative keys raise
    ``ValueError`` rather than silently producing an unreliable diagnosis.
    """

    try:
        root = ElementTree.fromstring(xml_data)
    except (ElementTree.ParseError, TypeError) as exc:
        raise ValueError("invalid Android SharedPreferences XML") from exc

    if root.tag.rsplit("}", 1)[-1] != "map":
        raise ValueError("Android SharedPreferences root must be <map>")

    known_keys = {spec.preference_key for spec in AI_DISPLAY_TOGGLES}
    parsed: dict[str, bool] = {}
    for element in root:
        if element.tag.rsplit("}", 1)[-1] != "boolean":
            continue
        preference_key = element.get("name")
        if preference_key not in known_keys:
            continue
        if preference_key in parsed:
            raise ValueError(
                f"duplicate SharedPreferences boolean: {preference_key}"
            )

        raw_value = element.get("value")
        normalized = raw_value.strip().lower() if raw_value is not None else ""
        if normalized == "true":
            parsed[preference_key] = True
        elif normalized == "false":
            parsed[preference_key] = False
        else:
            raise ValueError(
                f"invalid SharedPreferences boolean for {preference_key}: "
                f"{raw_value!r}"
            )

    return {
        spec.preference_key: parsed[spec.preference_key]
        for spec in AI_DISPLAY_TOGGLES
        if spec.preference_key in parsed
    }


def _attribute_value_span(
    xml_bytes: bytes,
    element_start: int,
    attribute_local_name: bytes,
) -> tuple[int, int]:
    """Locate one attribute's raw value inside a validated XML start tag."""

    whitespace = b" \t\r\n"
    length = len(xml_bytes)
    index = element_start + 1

    # Skip the element name.
    while index < length and xml_bytes[index] not in whitespace + b"/>":
        index += 1

    while index < length:
        while index < length and xml_bytes[index] in whitespace:
            index += 1
        if index >= length or xml_bytes[index] in b"/>":
            break

        name_start = index
        while index < length and xml_bytes[index] not in whitespace + b"=/>":
            index += 1
        raw_name = xml_bytes[name_start:index]
        while index < length and xml_bytes[index] in whitespace:
            index += 1
        if index >= length or xml_bytes[index] != ord("="):
            raise ValueError("malformed SharedPreferences attribute")
        index += 1
        while index < length and xml_bytes[index] in whitespace:
            index += 1
        if index >= length or xml_bytes[index] not in (ord('"'), ord("'")):
            raise ValueError("malformed SharedPreferences attribute value")

        quote = xml_bytes[index]
        value_start = index + 1
        value_end = xml_bytes.find(bytes((quote,)), value_start)
        if value_end < 0:
            raise ValueError("unterminated SharedPreferences attribute value")
        if raw_name.rsplit(b":", 1)[-1] == attribute_local_name:
            return value_start, value_end
        index = value_end + 1

    raise ValueError(
        f"missing {attribute_local_name.decode('ascii')} SharedPreferences attribute"
    )


def sync_ai_display_preferences_off(
    xml_data: str | bytes,
) -> SharedPreferencesSyncResult:
    """Safely synchronize the seven cached AI switch booleans to ``false``.

    The complete document is validated before any output is produced. All seven
    known keys must exist exactly once as direct ``<boolean>`` children of the
    SharedPreferences ``<map>``. The rewrite changes only the raw bytes inside
    their ``value`` attributes, preserving every unrelated entry, comment,
    declaration, whitespace character, attribute order, and quote style.

    Android SharedPreferences files are UTF-8. UTF-16/UTF-32 byte streams are
    rejected because an in-place ASCII attribute rewrite would not be safe.
    """

    if isinstance(xml_data, str):
        source = xml_data.encode("utf-8")
        return_bytes = False
    elif isinstance(xml_data, bytes):
        source = xml_data
        return_bytes = True
    else:
        raise ValueError("SharedPreferences XML must be str or bytes")

    if (
        source.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff"))
        or b"\x00" in source[:128]
    ):
        raise ValueError("only UTF-8 Android SharedPreferences XML is supported")
    if b"<!DOCTYPE" in source.upper():
        raise ValueError("DOCTYPE is not allowed in Android SharedPreferences XML")

    known_order = tuple(spec.preference_key for spec in AI_DISPLAY_TOGGLES)
    known_keys = set(known_order)
    seen: dict[str, tuple[bool, int]] = {}
    depth = 0
    root_is_map = False
    parser = expat.ParserCreate()

    def local_attribute(attributes: Mapping[str, str], name: str) -> str | None:
        matches = [
            value
            for attribute_name, value in attributes.items()
            if attribute_name.rsplit(":", 1)[-1] == name
        ]
        if len(matches) > 1:
            raise ValueError(f"duplicate {name} SharedPreferences attribute")
        return matches[0] if matches else None

    def start_element(name: str, attributes: dict[str, str]) -> None:
        nonlocal depth, root_is_map
        depth += 1
        local_name = name.rsplit(":", 1)[-1]
        if depth == 1:
            root_is_map = local_name == "map"

        preference_key = local_attribute(attributes, "name")
        if preference_key not in known_keys:
            return
        if preference_key in seen:
            raise ValueError(
                f"duplicate SharedPreferences preference: {preference_key}"
            )
        if depth != 2 or local_name != "boolean":
            raise ValueError(
                f"SharedPreferences key {preference_key} must be a direct boolean"
            )

        raw_value = local_attribute(attributes, "value")
        normalized = raw_value.strip().lower() if raw_value is not None else ""
        if normalized not in ("true", "false"):
            raise ValueError(
                f"invalid SharedPreferences boolean for {preference_key}: "
                f"{raw_value!r}"
            )
        seen[preference_key] = (
            normalized == "true",
            parser.CurrentByteIndex,
        )

    def end_element(_name: str) -> None:
        nonlocal depth
        depth -= 1

    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    try:
        parser.Parse(source, True)
    except expat.ExpatError as exc:
        raise ValueError("invalid Android SharedPreferences XML") from exc

    if not root_is_map:
        raise ValueError("Android SharedPreferences root must be <map>")
    missing_keys = tuple(key for key in known_order if key not in seen)
    if missing_keys:
        raise ValueError(
            "missing SharedPreferences booleans: " + ", ".join(missing_keys)
        )

    changed_keys = tuple(key for key in known_order if seen[key][0])
    if not changed_keys:
        return SharedPreferencesSyncResult(
            xml_data=xml_data,
            changed=False,
            changed_keys=(),
        )

    replacements = [
        _attribute_value_span(source, seen[key][1], b"value")
        for key in changed_keys
    ]
    transformed = source
    for value_start, value_end in sorted(replacements, reverse=True):
        transformed = (
            transformed[:value_start] + b"false" + transformed[value_end:]
        )

    return SharedPreferencesSyncResult(
        xml_data=(transformed if return_bytes else transformed.decode("utf-8")),
        changed=True,
        changed_keys=changed_keys,
    )


def find_stale_on_toggles(
    preference_booleans: Mapping[str, bool],
    raw_properties: Mapping[str, PropertyValue],
) -> tuple[StaleOnToggle, ...]:
    """Report cached-on switches whose corresponding runtime property is off.

    A preference is called stale only when its stored value is exactly ``True``
    and its property is exactly the firmware's declared off value.  Missing or
    unknown runtime values are not mislabeled as stale; the regular guard
    evaluator will instead report those values as unverifiable drift.
    """

    stale: list[StaleOnToggle] = []
    for spec in AI_DISPLAY_TOGGLES:
        if preference_booleans.get(spec.preference_key) is not True:
            continue
        runtime_value = normalize_property_value(
            raw_properties.get(spec.property_key)
        )
        if runtime_value == spec.off_value:
            stale.append(
                StaleOnToggle(
                    spec=spec,
                    preference_value=True,
                    runtime_value=runtime_value,
                )
            )
    return tuple(stale)


def find_stale_on_toggles_from_xml(
    xml_data: str | bytes,
    raw_properties: Mapping[str, PropertyValue],
) -> tuple[StaleOnToggle, ...]:
    """Parse a preference file and report its stale-on visible switches."""

    return find_stale_on_toggles(
        parse_shared_preferences_booleans(xml_data),
        raw_properties,
    )


def build_off_property_map() -> dict[str, str]:
    """Build the complete, ordered property-to-off-value correction map."""

    properties = {
        spec.property_key: spec.off_value for spec in AI_DISPLAY_TOGGLES
    }
    properties.update(EXTRA_OFF_PROPERTIES)

    # A defensive invariant against regressing the vendor capability behavior.
    if any(key in properties for key in PRESERVED_FIRMWARE_PROPERTIES):
        raise AssertionError("preserved firmware properties must not be forced off")
    return properties


def plan_guard_on_connection(
    device_id: str,
    raw_properties: Mapping[str, PropertyValue],
    *,
    automatic: bool = AUTO_ENFORCE_ON_CONNECT,
) -> GuardApplicationPlan:
    """Build a safe off-plan for any newly connected device.

    Presence of at least one authoritative toggle property identifies a
    compatible RK-style AI display implementation. A full device exposes all
    seven toggle properties; a partial device may safely receive only the known
    values it actually exposes. Empty or absent properties are explicitly
    skipped and are never treated as successfully fixed.
    """

    if not isinstance(device_id, str) or not device_id.strip():
        raise ValueError("device_id must be a non-empty opaque identifier")

    expected_properties = build_off_property_map()
    normalized_properties = {
        property_key: normalize_property_value(raw_properties.get(property_key))
        for property_key in expected_properties
    }
    toggle_keys = {spec.property_key for spec in AI_DISPLAY_TOGGLES}
    supported_toggle_keys = {
        property_key
        for property_key in toggle_keys
        if normalized_properties[property_key] is not None
    }

    if not supported_toggle_keys:
        capability = CapabilityLevel.UNSUPPORTED
    elif len(supported_toggle_keys) == len(toggle_keys):
        capability = CapabilityLevel.FULL
    else:
        capability = CapabilityLevel.PARTIAL

    toggle_names = {
        spec.property_key: spec.name for spec in AI_DISPLAY_TOGGLES
    }
    supported: list[PropertyEvaluation] = []
    skipped: list[SkippedProperty] = []
    for property_key, expected_value in expected_properties.items():
        raw_value = normalized_properties[property_key]
        if capability is CapabilityLevel.UNSUPPORTED:
            skipped.append(
                SkippedProperty(
                    property_key=property_key,
                    expected_value=expected_value,
                    reason=(
                        SkipReason.INCOMPATIBLE_FIRMWARE
                        if raw_value is not None
                        else SkipReason.UNSUPPORTED_OR_MISSING
                    ),
                    toggle_name=toggle_names.get(property_key),
                )
            )
        elif raw_value is None:
            skipped.append(
                SkippedProperty(
                    property_key=property_key,
                    expected_value=expected_value,
                    reason=SkipReason.UNSUPPORTED_OR_MISSING,
                    toggle_name=toggle_names.get(property_key),
                )
            )
        else:
            supported.append(
                PropertyEvaluation(
                    property_key=property_key,
                    raw_value=raw_value,
                    expected_value=expected_value,
                    drift=raw_value != expected_value,
                )
            )

    return GuardApplicationPlan(
        device_id=device_id.strip(),
        automatic=bool(automatic),
        capability=capability,
        supported_properties=tuple(supported),
        skipped_properties=tuple(skipped),
    )


def evaluate_toggle(
    spec: ToggleSpec,
    raw_properties: Mapping[str, PropertyValue],
) -> ToggleEvaluation:
    """Evaluate a single switch against a raw getprop mapping."""

    raw_value = normalize_property_value(raw_properties.get(spec.property_key))
    if raw_value is None:
        state = ToggleState.MISSING
    elif raw_value == spec.off_value:
        state = ToggleState.OFF
    elif raw_value in spec.on_values:
        state = ToggleState.ON
    else:
        state = ToggleState.UNKNOWN

    # Verification is intentionally stricter than the UI's checked-state test.
    return ToggleEvaluation(
        spec=spec,
        raw_value=raw_value,
        state=state,
        drift=raw_value != spec.off_value,
    )


def evaluate_getprop_mapping(
    raw_properties: Mapping[str, PropertyValue],
) -> GuardEvaluation:
    """Evaluate every authoritative switch and secondary off-property."""

    toggles = tuple(
        evaluate_toggle(spec, raw_properties) for spec in AI_DISPLAY_TOGGLES
    )
    extra_properties = tuple(
        PropertyEvaluation(
            property_key=property_key,
            raw_value=(raw_value := normalize_property_value(
                raw_properties.get(property_key)
            )),
            expected_value=expected_value,
            drift=raw_value != expected_value,
        )
        for property_key, expected_value in EXTRA_OFF_PROPERTIES
    )
    return GuardEvaluation(
        toggles=toggles,
        extra_properties=extra_properties,
    )


__all__ = (
    "AI_DISPLAY_TOGGLES",
    "AUTO_ENFORCE_ON_CONNECT",
    "CapabilityLevel",
    "EXTRA_OFF_PROPERTIES",
    "PRESERVED_FIRMWARE_PROPERTIES",
    "GuardApplicationPlan",
    "GuardEvaluation",
    "PropertyEvaluation",
    "SharedPreferencesSyncResult",
    "SkipReason",
    "SkippedProperty",
    "StaleOnToggle",
    "ToggleEvaluation",
    "ToggleSpec",
    "ToggleState",
    "build_off_property_map",
    "evaluate_getprop_mapping",
    "evaluate_toggle",
    "find_stale_on_toggles",
    "find_stale_on_toggles_from_xml",
    "normalize_property_value",
    "parse_shared_preferences_booleans",
    "plan_guard_on_connection",
    "sync_ai_display_preferences_off",
)
