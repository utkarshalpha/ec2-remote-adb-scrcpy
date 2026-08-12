#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Morning health check for one device.

The routine this replaces is manual: open a tunnel, attach scrcpy, start a
screen session, and look at the screen to decide whether the client will walk
in to a working display. That answers four questions, and ADB can answer all
four without a human watching:

    is the device reachable
    is the right application in front
    is anything actually playing, or is the screen black or frozen
    is a dialog covering it -- an update prompt, a permission, a crash

The checks reuse the analysis already proven in ``fleet_auditor`` rather than
growing a second copy of it. Nothing here imports Qt, so it can be tested
directly and driven from either the console auditor or the desktop app.

Deciding what *should* be in front is deliberately not automatic. Package names
follow the client (``com.cascades`` at a Cascades site), but a site can carry a
product with its own package, so the expected package is learned once from a
device the operator confirms is healthy and remembered per site after that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import time

from fleet_auditor import (
    analyze_frames,
    detect_popup,
    final_screen_state,
    parse_foreground_package,
)


# Verdicts, ordered worst first so a run can be sorted by what needs attention.
OFFLINE = "OFFLINE"
NEEDS_ATTENTION = "NEEDS ATTENTION"
UNCONFIRMED = "UNCONFIRMED"
HEALTHY = "HEALTHY"

VERDICT_ORDER = {OFFLINE: 0, NEEDS_ATTENTION: 1, UNCONFIRMED: 2, HEALTHY: 3}

CLEANUP_PACKAGE = "com.charon.rocketfly"

# Screen states that mean nobody would see a working display.
BROKEN_SCREEN_STATES = {
    "BLACK_SCREEN",
    "SCREENSHOT_UNAVAILABLE",
}
FROZEN_SCREEN_STATES = {
    "STATIC_OR_FROZEN",
    "VIDEO_FROZEN_OR_PROTECTED",
}

# Launchers and system surfaces. Seeing one of these in front means the client
# application is not running, whatever else the device reports.
LAUNCHER_HINTS = (
    "launcher",
    "com.android.systemui",
    "com.google.android.apps.tv.launcherx",
)


@dataclass
class HealthResult:
    """What one device looked like at one moment."""

    address: str = ""
    serial: str = ""
    model: str = ""
    project: str = ""
    reachable: bool = False
    foreground_package: str = ""
    app_version: str = ""
    expected_package: str = ""
    screen_state: str = ""
    media_playing: bool = False
    popup_text: str = ""
    cleanup_launched: bool = False
    verdict: str = UNCONFIRMED
    reasons: list[str] = field(default_factory=list)
    screenshot = None

    @property
    def needs_operator(self) -> bool:
        return self.verdict in (OFFLINE, NEEDS_ATTENTION)

    def summary(self) -> str:
        """One line, the way it would be read out on a morning call."""
        who = self.serial or self.address or "unknown device"
        if not self.reachable:
            return f"{who}: offline — no response over the tunnel"
        what = self.foreground_package or "nothing in front"
        state = {
            "VIDEO_PLAYING": "video playing",
            "SCREEN_MOVING": "screen moving",
            "BLACK_SCREEN": "black screen",
            "BLACK_OR_PROTECTED_VIDEO": "black or protected video",
            "STATIC_OR_FROZEN": "static or frozen",
            "VIDEO_FROZEN_OR_PROTECTED": "video frozen or protected",
            "SCREENSHOT_UNAVAILABLE": "screenshot unavailable",
        }.get(self.screen_state, self.screen_state or "unknown")
        line = f"{who}: {what} · {state}"
        if self.popup_text:
            line += " · popup"
        return line

    def daily_check_detail(self) -> str:
        """The Detailed Description block the Daily Check Log asks for."""
        return (
            f"Media Player Online: {'Yes' if self.reachable else 'No'}\n"
            f"App version: {self.app_version or 'unknown'}\n"
            f"Video running: {'Yes' if self.is_playing else 'No'}"
        )

    @property
    def is_playing(self) -> bool:
        return self.screen_state in ("VIDEO_PLAYING", "SCREEN_MOVING")


def looks_like_launcher(package: str) -> bool:
    lowered = (package or "").casefold()
    return any(hint in lowered for hint in LAUNCHER_HINTS)


def judge(result: HealthResult) -> HealthResult:
    """Turn the observations into a verdict and the reasons behind it.

    Kept separate from collection so the rules can be tested without a device,
    and so a stored result can be re-judged if the expectations change.
    """
    reasons: list[str] = []

    if not result.reachable:
        result.verdict = OFFLINE
        result.reasons = ["The device did not answer over the tunnel."]
        return result

    if result.popup_text:
        reasons.append(f"A dialog is covering the screen: {result.popup_text}")

    if result.expected_package:
        if not result.foreground_package:
            reasons.append("Nothing is in the foreground.")
        elif result.foreground_package != result.expected_package:
            reasons.append(
                f"{result.foreground_package} is in front, expected "
                f"{result.expected_package}.")
    elif looks_like_launcher(result.foreground_package):
        # No baseline yet, but a launcher in front is wrong on any signage unit.
        reasons.append(
            f"{result.foreground_package} looks like a launcher, so the client "
            "application is not running.")

    if result.screen_state in BROKEN_SCREEN_STATES:
        reasons.append(f"Screen reports {result.screen_state}.")
    elif result.screen_state in FROZEN_SCREEN_STATES:
        reasons.append(
            "The picture did not change between frames, so playback looks "
            "frozen." if not result.media_playing else
            "A media session is playing but the picture is not changing.")

    if reasons:
        result.verdict = NEEDS_ATTENTION
    elif not result.expected_package:
        # Everything observable looks fine, but nobody has said what should be
        # in front, so this is not yet a pass anyone should rely on.
        result.verdict = UNCONFIRMED
        reasons.append(
            f"{result.foreground_package} is in front and playing. Confirm it "
            "is the right application to remember it for this site.")
    else:
        result.verdict = HEALTHY

    result.reasons = reasons
    return result


class AdbRunner:
    """Minimal ADB surface so the engine is testable without a device."""

    def __init__(self, address, command_fn, screenshot_fn=None):
        self.address = address
        self._command = command_fn
        self._screenshot = screenshot_fn

    def shell(self, *args, timeout=25):
        code, output, _elapsed = self._command(
            "shell", *args, serial=self.address, timeout=timeout)
        return code, output or ""

    def getprop(self, name):
        _code, output = self.shell("getprop", name, timeout=12)
        return output.strip()

    def screenshot(self):
        return self._screenshot() if self._screenshot else None


def read_foreground(adb: AdbRunner) -> str:
    _code, window = adb.shell("dumpsys", "window", timeout=25)
    _code2, activity = adb.shell("dumpsys", "activity", "activities", timeout=25)
    return parse_foreground_package(window, activity) or ""


def read_version(adb: AdbRunner, package: str) -> str:
    if not package:
        return ""
    _code, output = adb.shell("dumpsys", "package", package, timeout=25)
    match = re.search(r"versionName=(\S+)", output or "")
    return match.group(1) if match else ""


def media_is_playing(adb: AdbRunner, package: str) -> bool:
    if not package:
        return False
    _code, output = adb.shell("dumpsys", "media_session", timeout=25)
    lowered = (output or "").casefold()
    target = package.casefold()
    for match in re.finditer(re.escape(target), lowered):
        section = lowered[max(0, match.start() - 800):match.end() + 1800]
        if re.search(r"state\s*=\s*3\b|state\s*=\s*playing\b", section):
            return True
    return False


def read_ui_text(adb: AdbRunner):
    """Dump the view hierarchy to find dialog text."""
    from xml.etree import ElementTree

    remote = "/data/local/tmp/convrse-health-window.xml"
    adb.shell("uiautomator", "dump", "--compressed", remote, timeout=25)
    code, xml_text = adb.shell("cat", remote, timeout=15)
    adb.shell("rm", "-f", remote, timeout=10)
    if code != 0 or "<hierarchy" not in (xml_text or ""):
        return [], set()
    try:
        root = ElementTree.fromstring(xml_text[xml_text.find("<hierarchy"):])
    except ElementTree.ParseError:
        return [], set()
    texts, packages = [], set()
    for node in root.iter("node"):
        package = node.attrib.get("package", "").strip()
        if package:
            packages.add(package)
        for key in ("text", "content-desc"):
            value = " ".join(node.attrib.get(key, "").split())
            if value and value not in texts:
                texts.append(value)
    return texts, packages


def launch_cleanup(adb: AdbRunner) -> bool:
    """Bring CleanUp up, as the operator would by hand on connecting."""
    code, output = adb.shell("pm", "path", CLEANUP_PACKAGE, timeout=20)
    if code != 0 or not (output or "").startswith("package:"):
        return False
    code, _output = adb.shell(
        "monkey", "-p", CLEANUP_PACKAGE, "-c",
        "android.intent.category.LAUNCHER", "1", timeout=30)
    return code == 0


def run_health_check(
    adb: AdbRunner,
    expected_package: str = "",
    with_cleanup: bool = False,
    frame_count: int = 3,
    frame_interval: float = 1.5,
    sleep_fn=time.sleep,
) -> HealthResult:
    """Look at one device and decide whether a client would see it working."""
    result = HealthResult(address=adb.address, expected_package=expected_package)

    result.serial = adb.getprop("ro.serialno")
    result.model = adb.getprop("ro.product.model")
    if not result.serial and not result.model:
        result.reachable = False
        return judge(result)
    result.reachable = True

    if with_cleanup:
        result.cleanup_launched = launch_cleanup(adb)
        # Give the launch a moment before deciding what is in front.
        sleep_fn(frame_interval)

    result.foreground_package = read_foreground(adb)
    result.app_version = read_version(adb, result.foreground_package)
    result.media_playing = media_is_playing(adb, result.foreground_package)

    frames = []
    for index in range(max(1, frame_count)):
        frame = adb.screenshot()
        if frame is not None:
            frames.append(frame)
        if index < frame_count - 1:
            sleep_fn(frame_interval)
    screen = analyze_frames(frames)
    result.screen_state = final_screen_state(screen, result.media_playing)
    result.screenshot = screen.last_frame

    texts, ui_packages = read_ui_text(adb)
    result.popup_text = detect_popup(
        result.foreground_package,
        expected_package or result.foreground_package,
        texts,
        ui_packages,
    )

    return judge(result)
