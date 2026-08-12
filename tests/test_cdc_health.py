"""Tests for the morning health check.

The verdict rules are the part an operator trusts, so they are tested without a
device: a wrong verdict is worse than no check at all, because it sends someone
to a site that was fine or leaves a black screen in front of a client.
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cdc_health as health


def result(**kwargs):
    base = dict(
        address="127.0.0.1:49215",
        serial="SN2026020201959",
        model="H96_Max_M9",
        reachable=True,
        foreground_package="com.cascades",
        app_version="1.6.8",
        expected_package="com.cascades",
        screen_state="VIDEO_PLAYING",
        media_playing=True,
        popup_text="",
    )
    base.update(kwargs)
    return health.judge(health.HealthResult(**base))


class VerdictTests(unittest.TestCase):
    def test_right_app_playing_is_healthy(self):
        outcome = result()
        self.assertEqual(outcome.verdict, health.HEALTHY)
        self.assertEqual(outcome.reasons, [])
        self.assertFalse(outcome.needs_operator)

    def test_unreachable_device_is_offline(self):
        outcome = result(reachable=False)
        self.assertEqual(outcome.verdict, health.OFFLINE)
        self.assertTrue(outcome.needs_operator)

    def test_offline_short_circuits_every_other_rule(self):
        """A device that never answered cannot also be 'wrong app'."""
        outcome = result(
            reachable=False, foreground_package="", screen_state="BLACK_SCREEN",
            popup_text="Update available")
        self.assertEqual(outcome.verdict, health.OFFLINE)
        self.assertEqual(len(outcome.reasons), 1)

    def test_wrong_application_in_front(self):
        outcome = result(foreground_package="com.example.hlauncher")
        self.assertEqual(outcome.verdict, health.NEEDS_ATTENTION)
        self.assertTrue(any("expected com.cascades" in r for r in outcome.reasons))

    def test_black_screen(self):
        outcome = result(screen_state="BLACK_SCREEN", media_playing=False)
        self.assertEqual(outcome.verdict, health.NEEDS_ATTENTION)
        self.assertTrue(any("BLACK_SCREEN" in r for r in outcome.reasons))

    def test_frozen_picture_even_while_a_media_session_claims_to_play(self):
        """The case that matters: the app says playing, the screen is stuck."""
        outcome = result(
            screen_state="VIDEO_FROZEN_OR_PROTECTED", media_playing=True)
        self.assertEqual(outcome.verdict, health.NEEDS_ATTENTION)
        self.assertTrue(any("not changing" in r for r in outcome.reasons))

    def test_popup_is_reported_with_its_text(self):
        outcome = result(popup_text="Update available. Install now?")
        self.assertEqual(outcome.verdict, health.NEEDS_ATTENTION)
        self.assertTrue(any("Update available" in r for r in outcome.reasons))

    def test_popup_is_flagged_even_when_everything_else_is_fine(self):
        outcome = result(
            popup_text="Allow access to photos?", screen_state="VIDEO_PLAYING")
        self.assertEqual(outcome.verdict, health.NEEDS_ATTENTION)

    def test_several_faults_are_all_reported(self):
        outcome = result(
            foreground_package="com.example.hlauncher",
            screen_state="BLACK_SCREEN",
            popup_text="System UI isn't responding")
        self.assertEqual(outcome.verdict, health.NEEDS_ATTENTION)
        self.assertEqual(len(outcome.reasons), 3)

    def test_without_a_baseline_a_good_looking_device_is_unconfirmed(self):
        """Nobody has said what should be in front, so this is not a pass."""
        outcome = result(expected_package="")
        self.assertEqual(outcome.verdict, health.UNCONFIRMED)
        self.assertFalse(outcome.needs_operator)
        self.assertTrue(any("Confirm it" in r for r in outcome.reasons))

    def test_a_launcher_is_wrong_even_without_a_baseline(self):
        outcome = result(
            expected_package="", foreground_package="com.example.hlauncher")
        self.assertEqual(outcome.verdict, health.NEEDS_ATTENTION)

    def test_nothing_in_the_foreground_is_a_fault(self):
        outcome = result(foreground_package="")
        self.assertEqual(outcome.verdict, health.NEEDS_ATTENTION)


class ReportingTests(unittest.TestCase):
    def test_summary_reads_like_a_morning_note(self):
        text = result().summary()
        self.assertIn("SN2026020201959", text)
        self.assertIn("com.cascades", text)
        self.assertIn("video playing", text)

    def test_summary_of_an_offline_device(self):
        self.assertIn("offline", result(reachable=False).summary())

    def test_summary_flags_a_popup(self):
        self.assertIn("popup", result(popup_text="Update available").summary())

    def test_daily_check_detail_matches_the_sheet(self):
        """The Daily Check Log asks for exactly these three lines."""
        detail = result().daily_check_detail()
        self.assertIn("Media Player Online: Yes", detail)
        self.assertIn("App version: 1.6.8", detail)
        self.assertIn("Video running: Yes", detail)

    def test_daily_check_detail_for_a_dead_device(self):
        detail = result(
            reachable=False, screen_state="", app_version="").daily_check_detail()
        self.assertIn("Media Player Online: No", detail)
        self.assertIn("App version: unknown", detail)
        self.assertIn("Video running: No", detail)

    def test_frozen_screen_is_not_reported_as_running(self):
        self.assertFalse(result(screen_state="STATIC_OR_FROZEN").is_playing)

    def test_verdicts_sort_worst_first(self):
        order = sorted(
            [health.HEALTHY, health.OFFLINE, health.UNCONFIRMED,
             health.NEEDS_ATTENTION],
            key=lambda v: health.VERDICT_ORDER[v])
        self.assertEqual(
            order,
            [health.OFFLINE, health.NEEDS_ATTENTION, health.UNCONFIRMED,
             health.HEALTHY])


class LauncherDetectionTests(unittest.TestCase):
    def test_known_launchers(self):
        for package in ("com.example.hlauncher", "com.android.systemui",
                        "com.google.android.apps.tv.launcherx"):
            self.assertTrue(health.looks_like_launcher(package), package)

    def test_client_applications_are_not_launchers(self):
        for package in ("com.cascades", "com.Shalimar", "com.charon.rocketfly"):
            self.assertFalse(health.looks_like_launcher(package), package)

    def test_blank_is_not_a_launcher(self):
        self.assertFalse(health.looks_like_launcher(""))


class FakeAdb:
    """Scripted ADB so a whole run can be exercised with no device."""

    def __init__(self, responses, screenshot=None):
        self.address = "127.0.0.1:49215"
        self.responses = responses
        self.calls = []
        self._screenshot = screenshot

    def shell(self, *args, timeout=25):
        self.calls.append(args)
        key = " ".join(str(a) for a in args)
        for prefix, value in self.responses.items():
            if key.startswith(prefix):
                return value
        return 1, ""

    def getprop(self, name):
        code, output = self.shell("getprop", name)
        return output.strip()

    def screenshot(self):
        return self._screenshot


class RunTests(unittest.TestCase):
    def test_an_unreachable_device_stops_immediately(self):
        adb = FakeAdb({})
        outcome = health.run_health_check(adb, sleep_fn=lambda _s: None)
        self.assertEqual(outcome.verdict, health.OFFLINE)
        self.assertFalse(outcome.reachable)
        # No point dumping the UI or grabbing frames from a device that is gone.
        self.assertFalse(any("uiautomator" in " ".join(c) for c in adb.calls))

    def test_a_reachable_device_is_identified_and_judged(self):
        adb = FakeAdb({
            "getprop ro.serialno": (0, "SN2026020201959\n"),
            "getprop ro.product.model": (0, "H96_Max_M9\n"),
            "dumpsys window": (0, "mCurrentFocus=Window{a u0 com.cascades/.Main}"),
            "dumpsys activity": (0, ""),
            "dumpsys package": (0, "    versionName=1.6.8\n"),
            "dumpsys media_session": (0, ""),
            "uiautomator": (0, ""),
            "cat ": (1, ""),
            "rm ": (0, ""),
        })
        outcome = health.run_health_check(
            adb, expected_package="com.cascades", sleep_fn=lambda _s: None)
        self.assertTrue(outcome.reachable)
        self.assertEqual(outcome.serial, "SN2026020201959")
        self.assertEqual(outcome.foreground_package, "com.cascades")
        self.assertEqual(outcome.app_version, "1.6.8")

    def test_cleanup_is_only_launched_when_asked(self):
        responses = {
            "getprop ro.serialno": (0, "SN1\n"),
            "getprop ro.product.model": (0, "H96\n"),
            "pm path": (0, "package:/data/app/cleanup.apk\n"),
            "monkey": (0, "ok"),
            "dumpsys window": (0, ""),
            "dumpsys activity": (0, ""),
            "dumpsys media_session": (0, ""),
            "uiautomator": (0, ""),
            "cat ": (1, ""),
            "rm ": (0, ""),
        }
        quiet = FakeAdb(dict(responses))
        health.run_health_check(quiet, sleep_fn=lambda _s: None)
        self.assertFalse(any("monkey" in " ".join(c) for c in quiet.calls))

        loud = FakeAdb(dict(responses))
        outcome = health.run_health_check(
            loud, with_cleanup=True, sleep_fn=lambda _s: None)
        self.assertTrue(any("monkey" in " ".join(c) for c in loud.calls))
        self.assertTrue(outcome.cleanup_launched)

    def test_cleanup_absent_is_reported_not_raised(self):
        adb = FakeAdb({
            "getprop ro.serialno": (0, "SN1\n"),
            "getprop ro.product.model": (0, "H96\n"),
            "pm path": (1, ""),
            "dumpsys window": (0, ""),
            "dumpsys activity": (0, ""),
            "dumpsys media_session": (0, ""),
            "uiautomator": (0, ""),
            "cat ": (1, ""),
            "rm ": (0, ""),
        })
        outcome = health.run_health_check(
            adb, with_cleanup=True, sleep_fn=lambda _s: None)
        self.assertFalse(outcome.cleanup_launched)


if __name__ == "__main__":
    unittest.main()
