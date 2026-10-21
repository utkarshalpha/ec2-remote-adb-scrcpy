#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tablet support for Convrse Device Control: power state and finding a device.

The fleet started as wall-mounted media players that are always plugged in and
always awake.  The tablets are neither.  Two things came up the first time one
was checked through the CDM tunnel:

* A Samsung tablet would not wake.  ``input keyevent WAKEUP`` reached it, but
  its log said ``Screen__On : Cancel`` and ``dumpsys power`` gave
  ``mLastSleepReason=cover_close``: its book cover was shut, and Samsung refuses
  a remote wake while it is.  Without the reason the operator is left pressing
  Power and wondering whether the tunnel works.
* Someone on site had to find the tablet among several.  Ringing it and putting
  a message on its screen did that, but only by hand.

Everything here builds shell text or parses shell output, so it can be tested
without a device and without Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex


# One round trip for everything the status line needs.  ``dumpsys battery`` is
# the authoritative source for level and charger; ``dumpsys power`` carries the
# screen state and, on Samsung, the reason the screen last went off.
STATUS_COMMAND = (
    "echo @@battery; dumpsys battery 2>/dev/null"
    " | grep -E '^ *(level|scale|status|AC powered|USB powered|Wireless powered):';"
    " echo @@power; dumpsys power 2>/dev/null"
    " | grep -E 'mWakefulness=|mLastSleepReason=|mIsPowered=';"
    " echo @@rotation; echo auto=$(settings get system accelerometer_rotation);"
    " echo user=$(settings get system user_rotation)"
)

# BatteryManager.BATTERY_STATUS_* values.
_STATUS_CHARGING = 2
_STATUS_FULL = 5


@dataclass
class PowerStatus:
    battery_percent: int | None = None
    charging: bool | None = None
    plugged: bool | None = None
    wakefulness: str = ""
    last_sleep_reason: str = ""
    auto_rotate: bool | None = None
    user_rotation: int | None = None

    @property
    def screen_on(self) -> bool | None:
        if not self.wakefulness:
            return None
        return self.wakefulness.lower() == "awake"

    @property
    def cover_closed(self) -> bool:
        """True when the screen is off because a flip cover was shut."""
        return (self.screen_on is False
                and "cover" in self.last_sleep_reason.lower())

    @property
    def low_battery(self) -> bool:
        return (self.battery_percent is not None
                and self.battery_percent <= 20
                and not self.plugged)

    def battery_text(self) -> str:
        if self.battery_percent is None:
            return ""
        if self.charging:
            state = "charging"
        elif self.plugged:
            state = "plugged in"
        else:
            state = "not charging"
        return f"{self.battery_percent}% · {state}"

    def screen_text(self) -> str:
        if self.screen_on is None:
            return ""
        if self.screen_on:
            return "On"
        if self.cover_closed:
            return "Off · cover closed"
        if self.wakefulness.lower() == "dozing":
            return "Off · dozing"
        return "Off"

    def summary(self) -> str:
        """One line for the status strip, empty when nothing was read."""
        parts = []
        battery = self.battery_text()
        if battery:
            parts.append(f"Battery {battery}")
        screen = self.screen_text()
        if screen:
            parts.append(f"Screen {screen.lower()}")
        return "  ·  ".join(parts)

    def wake_blocked_reason(self) -> str:
        """Why a remote wake will not work, in words an operator can act on."""
        if self.cover_closed:
            return ("The tablet's cover is closed. Samsung tablets refuse to "
                    "turn the screen on remotely while it is shut. Ask someone "
                    "on site to open the cover.")
        return ""


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("@@"):
            current = line[2:]
            sections.setdefault(current, [])
        elif current and line:
            sections[current].append(line)
    return sections


def parse_status(text: str) -> PowerStatus:
    """Parse the output of ``STATUS_COMMAND``.  Missing values stay unknown."""
    status = PowerStatus()
    sections = _sections(text)

    battery: dict[str, str] = {}
    for line in sections.get("battery", []):
        key, sep, value = line.partition(":")
        if sep:
            battery[key.strip().lower()] = value.strip()
    try:
        level = int(battery["level"])
        scale = int(battery.get("scale") or 100) or 100
        status.battery_percent = max(0, min(100, round(level * 100 / scale)))
    except (KeyError, ValueError):
        pass
    sources = ("ac powered", "usb powered", "wireless powered")
    if any(key in battery for key in sources):
        status.plugged = any(battery.get(key) == "true" for key in sources)
    try:
        code = int(battery["status"])
        status.charging = code in (_STATUS_CHARGING, _STATUS_FULL) and bool(status.plugged)
    except (KeyError, ValueError):
        if status.plugged is not None:
            status.charging = status.plugged

    for line in sections.get("power", []):
        match = re.search(r"mWakefulness=(\w+)", line)
        if match and not status.wakefulness:
            status.wakefulness = match.group(1)
        match = re.search(r"mLastSleepReason=(\S+)", line)
        if match and not status.last_sleep_reason:
            status.last_sleep_reason = match.group(1)
        match = re.search(r"mIsPowered=(true|false)", line)
        if match and status.plugged is None:
            status.plugged = match.group(1) == "true"
            if status.charging is None:
                status.charging = status.plugged
    for line in sections.get("rotation", []):
        key, _, value = line.partition("=")
        if key == "auto" and value in ("0", "1"):
            status.auto_rotate = value == "1"
        elif key == "user" and value.isdigit():
            status.user_rotation = int(value) % 4
    return status


# ------------------------------------------------------------ tablets ----

# Samsung's and Lenovo's tablet model prefixes, for builds that leave
# ro.build.characteristics at "default".
_TABLET_MODEL = re.compile(r"^(SM-[TXP]\d|TB[-_]?\d)", re.IGNORECASE)


def is_tablet(characteristics: str, model: str = "") -> bool:
    """Whether the device is one of the fleet's tablets, not a media player.

    Android sets ro.build.characteristics to "tablet" on tablets; the media
    players report "default", "tv" or "box".
    """
    traits = {part.strip().lower() for part in (characteristics or "").split(",")}
    if "tablet" in traits:
        return True
    if traits & {"tv", "box", "watch", "automotive"}:
        return False
    return bool(_TABLET_MODEL.match((model or "").strip()))


def auto_rotate_command(enabled: bool) -> str:
    return f"settings put system accelerometer_rotation {1 if enabled else 0}"


def rotate_command(current: int | None) -> str:
    """Turn the screen a quarter turn and hold it there.

    Rotation is a step rather than Portrait/Landscape buttons because these
    tablets disagree about their natural orientation: rotation 0 is portrait on
    one model and landscape on another.
    """
    following = ((current or 0) + 1) % 4
    return ("settings put system accelerometer_rotation 0;"
            f" settings put system user_rotation {following}")


WAKE_COMMAND = "input keyevent KEYCODE_WAKEUP"
LOCK_COMMAND = "input keyevent KEYCODE_SLEEP"


# ------------------------------------------------------------- finding ----

FIND_NOTIFICATION_TAG = "cdcfind"

# Where the tone lives differs by maker: the Lenovo keeps AOSP's tones in
# /product/media/audio/ringtones, the Samsung nests its own under
# /system/media/audio/ringtones/SoundTheme/<theme>/.  The first known file
# present is used, then any ringtone at all; which tone does not matter, only
# that it is loud and unmistakable.
RINGTONE_CANDIDATES = (
    "/product/media/audio/ringtones/Atria.ogg",
    "/system/media/audio/ringtones/Atria.ogg",
    "/system/media/audio/ringtones/SoundTheme/Galaxy/Over_the_Horizon.ogg",
    "/system/media/audio/ringtones/SoundTheme/Galaxy/Galaxy_Bells.ogg",
)

MEDIA_STREAM = 3          # AudioManager.STREAM_MUSIC


def ringtone_lookup_command() -> str:
    """Print the first ringtone file that exists, or any ringtone at all."""
    tests = " ".join(shlex.quote(path) for path in RINGTONE_CANDIDATES)
    return (
        f"for f in {tests}; do [ -f \"$f\" ] && echo \"$f\" && exit 0; done;"
        " find /product/media/audio/ringtones /system/media/audio/ringtones"
        " -iname '*.ogg' 2>/dev/null | head -1"
    )


def player_lookup_command(path: str) -> str:
    """List the activities that can play ``path``."""
    return ("cmd package query-activities --brief -a android.intent.action.VIEW"
            f" -d {shlex.quote('file://' + path)} -t audio/ogg")


def parse_player_component(text: str) -> str:
    """Pick a player from ``query-activities --brief`` output.

    Without a component Android shows its "Open with" chooser instead of
    playing anything, which is what happened on the Lenovo tablet: a picker
    popped up over the client's screen and nothing rang.
    """
    components = re.findall(r"^\s*([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)\s*$",
                            text or "", re.MULTILINE)
    return components[0] if components else ""


def volume_get_command() -> str:
    return f"cmd media_session volume --stream {MEDIA_STREAM} --get"


def parse_volume(text: str) -> int | None:
    match = re.search(r"volume is (\d+) in range \[\d+\.\.(\d+)\]", text or "")
    return int(match.group(1)) if match else None


def parse_max_volume(text: str) -> int | None:
    match = re.search(r"in range \[\d+\.\.(\d+)\]", text or "")
    return int(match.group(1)) if match else None


def volume_set_command(level: int) -> str:
    return f"cmd media_session volume --stream {MEDIA_STREAM} --set {int(level)}"


def ring_once_command(component: str, path: str, volume: int | None) -> str:
    """Start the tone once.  ``-S`` restarts the player so it plays again."""
    parts = []
    if volume is not None:
        parts.append(volume_set_command(volume) + " >/dev/null 2>&1")
    target = f"-n {shlex.quote(component)} " if component else ""
    parts.append(
        f"am start -S {target}-a android.intent.action.VIEW"
        f" -d {shlex.quote('file://' + path)} -t audio/ogg >/dev/null 2>&1")
    return "; ".join(parts)


def stop_ring_command(component: str, restore_volume: int | None) -> str:
    parts = []
    package = component.split("/", 1)[0] if component else ""
    if package:
        parts.append(f"am force-stop {shlex.quote(package)}")
    if restore_volume is not None:
        parts.append(volume_set_command(restore_volume) + " >/dev/null 2>&1")
    return "; ".join(parts) or "true"


def find_message_command(label: str) -> str:
    """Post a notification and pull the shade down so it reads from a distance."""
    title = "CDM - THIS DEVICE"
    body = f"Convrse Device Control is looking for this device. {label}".strip()
    return (
        "input keyevent KEYCODE_WAKEUP;"
        f" cmd notification post -S bigtext -t {shlex.quote(title)}"
        f" {FIND_NOTIFICATION_TAG} {shlex.quote(body)} >/dev/null 2>&1;"
        " cmd statusbar expand-notifications"
    )


def clear_message_command() -> str:
    """Close the shade and drop the message.

    ``cmd notification`` has no cancel verb on these builds.  The notification
    belongs to com.android.shell, and stopping that package removes it.
    """
    return "cmd statusbar collapse; am force-stop com.android.shell"
