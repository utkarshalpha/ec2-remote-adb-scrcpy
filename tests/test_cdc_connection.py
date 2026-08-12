"""Tests for key storage and the local/leased port split."""

import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cdc_connection as conn


SAMPLE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAxV3mockmockmockmockmockmockmockmockmockmockmockmo\n"
    "ckmockmockmockmockmockmockmockmockmockmockmockmockmockmockmockmoc\n"
    "-----END RSA PRIVATE KEY-----\n"
)


class KeyTextTests(unittest.TestCase):
    def test_accepts_a_well_formed_key(self):
        self.assertTrue(conn.normalize_key_text(SAMPLE_KEY).endswith("\n"))

    def test_strips_crlf_that_chat_apps_introduce(self):
        pasted = SAMPLE_KEY.replace("\n", "\r\n")
        self.assertNotIn("\r", conn.normalize_key_text(pasted))

    def test_tolerates_surrounding_whitespace(self):
        normalized = conn.normalize_key_text("\n\n   " + SAMPLE_KEY + "   \n\n")
        self.assertTrue(normalized.startswith("-----BEGIN"))

    def test_rejects_empty_input(self):
        with self.assertRaises(conn.KeyImportError):
            conn.normalize_key_text("   ")

    def test_rejects_the_public_half_with_a_useful_message(self):
        with self.assertRaises(conn.KeyImportError) as caught:
            conn.normalize_key_text("ssh-rsa AAAAB3NzaC1yc2E user@host")
        self.assertIn("public half", str(caught.exception))

    def test_rejects_a_truncated_key(self):
        with self.assertRaises(conn.KeyImportError) as caught:
            conn.normalize_key_text(SAMPLE_KEY.split("-----END")[0])
        self.assertIn("closing", str(caught.exception))

    def test_accepts_openssh_and_ec_armour(self):
        for label in ("OPENSSH", "EC", "ENCRYPTED"):
            body = (f"-----BEGIN {label} PRIVATE KEY-----\nAAAA\n"
                    f"-----END {label} PRIVATE KEY-----\n")
            self.assertTrue(conn.normalize_key_text(body))


class KeyStorageTests(unittest.TestCase):
    def setUp(self):
        self._temp = TemporaryDirectory()
        patcher = mock.patch.object(
            conn, "app_data_dir", return_value=Path(self._temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temp.cleanup)

    def test_import_round_trip(self):
        self.assertFalse(conn.has_stored_key())
        path = conn.import_key_text(SAMPLE_KEY)
        self.assertTrue(conn.has_stored_key())
        self.assertEqual(path.read_text(encoding="ascii"), SAMPLE_KEY)

    def test_import_from_file_copies_it(self):
        source = Path(self._temp.name) / "picked.pem"
        source.write_text(SAMPLE_KEY, encoding="ascii")
        conn.import_key_file(source)
        self.assertTrue(conn.has_stored_key())
        # The original may be deleted afterwards without breaking the app.
        source.unlink()
        self.assertTrue(conn.has_stored_key())

    def test_missing_file_reports_clearly(self):
        with self.assertRaises(conn.KeyImportError):
            conn.import_key_file(Path(self._temp.name) / "nope.pem")

    def test_fingerprint_is_stable_and_not_the_key(self):
        conn.import_key_text(SAMPLE_KEY)
        hint = conn.key_fingerprint_hint()
        self.assertEqual(hint, conn.key_fingerprint_hint())
        self.assertTrue(hint)
        self.assertNotIn(hint.replace(":", ""), SAMPLE_KEY)

    def test_forget_removes_it(self):
        conn.import_key_text(SAMPLE_KEY)
        conn.forget_key()
        self.assertFalse(conn.has_stored_key())


class PortParsingTests(unittest.TestCase):
    def test_plain_port(self):
        self.assertEqual(conn.parse_endpoint("17002"), 17002)

    def test_host_and_port_as_copied_from_the_website(self):
        self.assertEqual(conn.parse_endpoint("cdm.convrse.ai:17002"), 17002)

    def test_a_whole_ssh_command_pasted_in(self):
        pasted = "ssh -i key.pem -L 17002:localhost:17002 ubuntu@cdm.convrse.ai"
        self.assertEqual(conn.parse_endpoint(pasted), 17002)

    def test_blank_and_nonsense_are_rejected(self):
        for value in ("", "   ", "abc", None):
            self.assertIsNone(conn.parse_endpoint(value))

    def test_out_of_range_is_rejected(self):
        self.assertIsNone(conn.validated_port("70000"))
        self.assertIsNone(conn.validated_port("0"))
        self.assertIsNone(conn.validated_port("-1"))


class LocalPortTests(unittest.TestCase):
    def test_finds_a_bindable_port(self):
        port = conn.find_free_local_port()
        self.assertTrue(conn.can_bind(port))
        low, high = conn.LOCAL_PORT_RANGE
        self.assertTrue(low <= port <= high)

    def test_skips_a_port_already_bound(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            taken = held.getsockname()[1]
            held.listen(1)
            self.assertFalse(conn.can_bind(taken))
            self.assertNotEqual(conn.find_free_local_port(preferred=taken), taken)

    def test_two_calls_can_be_held_at_once(self):
        """Two operators on one machine must not be handed the same socket."""
        first = conn.find_free_local_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", first))
            held.listen(1)
            second = conn.find_free_local_port()
            self.assertNotEqual(first, second)


class SshCommandTests(unittest.TestCase):
    def test_local_and_leased_ports_stay_separate(self):
        command = conn.build_ssh_command(
            "ssh", "C:/key.pem", 49215, 17002, "ubuntu@cdm.convrse.ai")
        self.assertIn("-L", command)
        forward = command[command.index("-L") + 1]
        self.assertEqual(forward, "127.0.0.1:49215:127.0.0.1:17002")

    def test_forward_target_is_ipv4_not_localhost(self):
        """On the gateway, localhost resolves to ::1 while the tunnel listeners
        bind 0.0.0.0, so naming 127.0.0.1 avoids a refused IPv6 attempt."""
        command = conn.build_ssh_command(
            "ssh", "C:/key.pem", 49215, 17002, "ubuntu@cdm.convrse.ai")
        forward = command[command.index("-L") + 1]
        self.assertNotIn("localhost", forward)

    def test_forward_is_not_the_old_symmetric_form(self):
        """V2.3 forwarded {port}:localhost:{port}, which is what made two
        operators who both typed 17000 share one device."""
        command = conn.build_ssh_command(
            "ssh", "C:/key.pem", 49215, 17002, "ubuntu@cdm.convrse.ai")
        forward = command[command.index("-L") + 1]
        local, _bind, remote = forward.rsplit(":", 2)[0], None, forward.rsplit(":", 1)[1]
        self.assertNotEqual(local.split(":")[-1], remote)

    def test_binds_the_forward_to_loopback_only(self):
        command = conn.build_ssh_command(
            "ssh", "C:/key.pem", 49215, 17002, "ubuntu@cdm.convrse.ai")
        forward = command[command.index("-L") + 1]
        self.assertTrue(forward.startswith("127.0.0.1:"))

    def test_non_interactive_so_it_cannot_hang_on_a_prompt(self):
        command = conn.build_ssh_command(
            "ssh", "C:/key.pem", 49215, 17002, "ubuntu@cdm.convrse.ai")
        self.assertIn("BatchMode=yes", command)
        self.assertIn("ExitOnForwardFailure=yes", command)


class RouteDescriptionTests(unittest.TestCase):
    def test_shows_both_numbers(self):
        text = conn.describe_route(49215, 17002, "ubuntu@cdm.convrse.ai")
        self.assertIn("49215", text)
        self.assertIn("17002", text)
        self.assertIn("cdm.convrse.ai", text)
        self.assertNotIn("ubuntu@", text)

    def test_idle_state(self):
        self.assertEqual(
            conn.describe_route(None, None, "ubuntu@cdm.convrse.ai"),
            "Not connected")


class DeviceIdentityTests(unittest.TestCase):
    def test_label_matches_what_the_portal_shows(self):
        """The website shows 'SN2026020201959 / neopolis'; so do we."""
        identity = conn.DeviceIdentity(
            serial="SN2026020201959", model="H96_Max_M9",
            name="rk3576_box", project="neopolis")
        self.assertEqual(identity.label(), "SN2026020201959 / neopolis")

    def test_serial_leads_even_without_a_project(self):
        identity = conn.DeviceIdentity(
            serial="SN2026020201959", model="H96_Max_M9", name="rk3576_box")
        self.assertEqual(identity.label(), "SN2026020201959")

    def test_hardware_name_never_masks_the_serial(self):
        """rk3576_box is identical on every unit, so it cannot lead."""
        identity = conn.DeviceIdentity(
            serial="SN2026020201959", name="rk3576_box")
        self.assertTrue(identity.label().startswith("SN2026020201959"))
        self.assertNotIn("rk3576_box", identity.label())

    def test_falls_back_to_model_when_there_is_no_serial(self):
        self.assertEqual(conn.DeviceIdentity(model="H96_Max_M9").label(), "H96_Max_M9")

    def test_details_carry_everything_for_the_tooltip(self):
        identity = conn.DeviceIdentity(
            serial="SN1", model="H96", name="rk3576_box",
            android="14.0.1", project="neopolis")
        details = identity.details()
        for expected in ("SN1", "neopolis", "H96", "rk3576_box", "14.0.1"):
            self.assertIn(expected, details)

    def test_empty_identity_is_falsey(self):
        self.assertFalse(conn.DeviceIdentity())


if __name__ == "__main__":
    unittest.main()
