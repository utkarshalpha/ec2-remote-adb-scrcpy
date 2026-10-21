"""Tests for tablet power state, finding a device, and the gateway route.

The shell outputs below are what the two tablets on the fleet returned through
the CDM tunnel on 2026-10-21: a Samsung SM-X216B with its cover shut, and a
Lenovo TB390FU.
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cdc_connection as conn
import cdc_tablet as tablet


SAMSUNG_COVER_CLOSED = """\
@@battery
  AC powered: false
  USB powered: false
  Wireless powered: false
  status: 3
  level: 30
  scale: 100
@@power
  mWakefulness=Dozing
  mIsPowered=false
  mLastSleepReason=cover_close
  mIsPowered=false
"""

LENOVO_AWAKE = """\
@@battery
  AC powered: false
  USB powered: false
  Wireless powered: false
  status: 3
  level: 71
  scale: 100
@@power
  mWakefulness=Awake
  mIsPowered=false
"""

CHARGING = """\
@@battery
  AC powered: true
  USB powered: false
  status: 2
  level: 54
  scale: 100
@@power
  mWakefulness=Awake
  mIsPowered=true
"""

LENOVO_PLAYERS = """\
2 activities found:
  Activity #0:
    priority=0 preferredOrder=0 match=0x608000 specificIndex=-1 isDefault=true
    com.google.android.apps.nbu.files/.gateway.preview.PreviewActivity
  Activity #1:
    priority=0 preferredOrder=0 match=0x608000 specificIndex=-1 isDefault=true
    com.google.android.apps.youtube.music/.audiopreview.AudioPreviewPlayerActivity
"""


class PowerStatusTests(unittest.TestCase):
    def test_samsung_with_its_cover_shut(self):
        status = tablet.parse_status(SAMSUNG_COVER_CLOSED)
        self.assertEqual(status.battery_percent, 30)
        self.assertFalse(status.plugged)
        self.assertFalse(status.charging)
        self.assertFalse(status.screen_on)
        self.assertTrue(status.cover_closed)
        self.assertEqual(status.screen_text(), "Off · cover closed")
        self.assertIn("cover is closed", status.wake_blocked_reason())

    def test_cover_reason_is_ignored_once_the_screen_is_on(self):
        # Samsung keeps mLastSleepReason after the cover is opened again.
        status = tablet.parse_status(
            SAMSUNG_COVER_CLOSED.replace("Dozing", "Awake"))
        self.assertTrue(status.screen_on)
        self.assertFalse(status.cover_closed)
        self.assertEqual(status.wake_blocked_reason(), "")

    def test_lenovo_awake_on_battery(self):
        status = tablet.parse_status(LENOVO_AWAKE)
        self.assertEqual(status.battery_text(), "71% · not charging")
        self.assertEqual(status.screen_text(), "On")
        self.assertFalse(status.low_battery)
        self.assertEqual(status.summary(), "Battery 71% · not charging  ·  Screen on")

    def test_charging(self):
        status = tablet.parse_status(CHARGING)
        self.assertTrue(status.plugged)
        self.assertTrue(status.charging)
        self.assertEqual(status.battery_text(), "54% · charging")

    def test_low_battery_only_when_unplugged(self):
        low = tablet.parse_status(LENOVO_AWAKE.replace("level: 71", "level: 12"))
        self.assertTrue(low.low_battery)
        plugged = tablet.parse_status(CHARGING.replace("level: 54", "level: 12"))
        self.assertFalse(plugged.low_battery)

    def test_media_player_without_a_battery_reports_nothing_odd(self):
        status = tablet.parse_status("@@battery\n@@power\n  mWakefulness=Awake\n")
        self.assertIsNone(status.battery_percent)
        self.assertEqual(status.battery_text(), "")
        self.assertEqual(status.summary(), "Screen on")

    def test_empty_output(self):
        status = tablet.parse_status("")
        self.assertEqual(status.summary(), "")
        self.assertIsNone(status.screen_on)


class FindDeviceTests(unittest.TestCase):
    def test_picks_the_first_player_so_no_chooser_appears(self):
        self.assertEqual(
            tablet.parse_player_component(LENOVO_PLAYERS),
            "com.google.android.apps.nbu.files/.gateway.preview.PreviewActivity")

    def test_no_player(self):
        self.assertEqual(tablet.parse_player_component("No activities found"), "")

    def test_volume_parsing(self):
        text = "[V] Connecting to AudioService\n[V] volume is 12 in range [0..15]"
        self.assertEqual(tablet.parse_volume(text), 12)
        self.assertEqual(tablet.parse_max_volume(text), 15)
        self.assertIsNone(tablet.parse_volume("error"))

    def test_ring_command_names_the_player_and_restarts_it(self):
        script = tablet.ring_once_command(
            "com.example/.Player", "/product/media/audio/ringtones/Atria.ogg", 15)
        self.assertIn("--set 15", script)
        self.assertIn("am start -S -n com.example/.Player", script)
        self.assertIn("file:///product/media/audio/ringtones/Atria.ogg", script)

    def test_stop_puts_the_volume_back(self):
        script = tablet.stop_ring_command("com.example/.Player", 12)
        self.assertIn("am force-stop com.example", script)
        self.assertIn("--set 12", script)

    def test_ringtone_lookup_covers_both_layouts(self):
        script = tablet.ringtone_lookup_command()
        self.assertIn("/product/media/audio/ringtones", script)
        self.assertIn("SoundTheme", script)

    def test_message_opens_the_shade_and_clear_closes_it(self):
        self.assertIn("expand-notifications",
                      tablet.find_message_command("Serial R9ZY70QLQ7N"))
        self.assertIn("R9ZY70QLQ7N",
                      tablet.find_message_command("Serial R9ZY70QLQ7N"))
        self.assertIn("collapse", tablet.clear_message_command())

    def test_message_text_is_quoted_for_the_device_shell(self):
        script = tablet.find_message_command("Serial X; reboot")
        self.assertIn("'Convrse Device Control is looking for this device. "
                      "Serial X; reboot'", script)


class GatewayRouteTests(unittest.TestCase):
    def test_forwards_the_gateway_adb_server_and_scrcpy(self):
        command = conn.build_gateway_ssh_command(
            "ssh", "key.pem", 49300, 27190, "ubuntu@cdm.convrse.ai")
        self.assertIn("127.0.0.1:49300:127.0.0.1:5038", command)
        self.assertIn("127.0.0.1:27190:127.0.0.1:27190", command)
        self.assertIn("BatchMode=yes", command)
        self.assertEqual(command[-1], "ubuntu@cdm.convrse.ai")

    def test_device_is_named_from_the_gateway_side(self):
        self.assertEqual(conn.gateway_device_address(17000), "127.0.0.1:17000")
        self.assertEqual(conn.gateway_server_socket(49300), "tcp:127.0.0.1:49300")

    def test_scrcpy_uses_forward_mode_on_the_shared_port(self):
        self.assertEqual(conn.gateway_scrcpy_args(27190),
                         ["--force-adb-forward", "--port=27190"])
        self.assertEqual(conn.gateway_scrcpy_args(None), [])

    def test_scrcpy_port_comes_from_scrcpys_range(self):
        port = conn.find_free_scrcpy_port()
        low, high = conn.SCRCPY_PORT_RANGE
        self.assertTrue(low <= port <= high or port > 1024)


class TabletDetectionTests(unittest.TestCase):
    def test_the_fleets_tablets(self):
        self.assertTrue(tablet.is_tablet("tablet", "SM-X216B"))
        self.assertTrue(tablet.is_tablet("default", "TB390FU"))
        self.assertTrue(tablet.is_tablet("", "SM-T505"))

    def test_media_players_are_not_tablets(self):
        self.assertFalse(tablet.is_tablet("default", "H96_Max_M9"))
        self.assertFalse(tablet.is_tablet("tv", "rk3576_box"))
        self.assertFalse(tablet.is_tablet("", ""))

    def test_a_tv_build_wins_over_the_model_name(self):
        self.assertFalse(tablet.is_tablet("tv,nosdcard", "TB390FU"))


class RotationTests(unittest.TestCase):
    def test_rotation_is_parsed(self):
        status = tablet.parse_status(
            SAMSUNG_COVER_CLOSED + "@@rotation\nauto=0\nuser=1\n")
        self.assertFalse(status.auto_rotate)
        self.assertEqual(status.user_rotation, 1)

    def test_missing_rotation_stays_unknown(self):
        status = tablet.parse_status(
            LENOVO_AWAKE + "@@rotation\nauto=null\nuser=null\n")
        self.assertIsNone(status.auto_rotate)
        self.assertIsNone(status.user_rotation)

    def test_rotate_steps_a_quarter_turn_and_locks(self):
        self.assertIn("user_rotation 2", tablet.rotate_command(1))
        self.assertIn("user_rotation 0", tablet.rotate_command(3))
        self.assertIn("user_rotation 1", tablet.rotate_command(None))
        self.assertIn("accelerometer_rotation 0", tablet.rotate_command(0))

    def test_auto_rotate_toggle(self):
        self.assertTrue(tablet.auto_rotate_command(True).endswith(" 1"))
        self.assertTrue(tablet.auto_rotate_command(False).endswith(" 0"))


if __name__ == "__main__":
    unittest.main()
