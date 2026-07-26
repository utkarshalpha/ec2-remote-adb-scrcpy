import os
import hashlib
from pathlib import Path
import sys
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cdc_macos
import cdc_v2


class MacStreamCommandTests(unittest.TestCase):
    def setUp(self):
        self.config = cdc_v2.ResolvedStreamConfig(
            name="Normal",
            resolution="1280 × 720",
            max_size=1280,
            bitrate_bps=5_000_000,
            max_fps=30,
            h264_encoder="c2.rk.avc.encoder",
        )

    def test_native_command_keeps_reviewed_video_and_audio_settings(self):
        command = cdc_macos.build_native_scrcpy_command(
            "127.0.0.1:17000", "CDC Mac Mirror", self.config)
        joined = " ".join(str(part) for part in command)

        self.assertIn("--video-codec h264", joined)
        self.assertIn("--video-bit-rate 5000000", joined)
        self.assertIn("--max-fps 30", joined)
        self.assertIn("--audio-source output", joined)
        self.assertIn("--audio-codec opus", joined)
        self.assertIn("--audio-bit-rate 128K", joined)
        self.assertIn("--audio-buffer 200", joined)
        self.assertIn("--window-title CDC Mac Mirror", joined)

    def test_native_command_removes_every_windows_embedding_flag(self):
        command = cdc_macos.build_native_scrcpy_command(
            "127.0.0.1:17000", "CDC Mac Mirror", self.config)

        self.assertNotIn("--window-borderless", command)
        self.assertNotIn("--window-x", command)
        self.assertNotIn("--window-y", command)
        self.assertNotIn("4000", command)

    def test_mac_package_files_do_not_replace_windows_v23(self):
        self.assertTrue((PROJECT_ROOT / "scrcpy-remote-v2.3.spec").is_file())
        self.assertTrue((PROJECT_ROOT / "scrcpy-remote-macos-arm64.spec").is_file())
        self.assertTrue((PROJECT_ROOT / "build-macos-arm64.sh").is_file())

    def test_staged_scrcpy_runtime_matches_official_v41_files(self):
        expected = {
            "scrcpy": "e318a04c11986d9afa7f438a81cc9c7cc0f3ea66945db1e127f373eb02f4e1d3",
            "adb": "9fdf861259dc807937b13afdd5f053c7fda9f3b7726933fe0e0f45130ecb8dc7",
            "scrcpy-server": "deacb991ed2509715160ffdc7907e47b4160eb30d1566217e9047fd5b8850cae",
        }
        runtime_root = PROJECT_ROOT / "scrcpy-runtime-macos-arm64"
        if not runtime_root.is_dir():
            self.skipTest("The downloaded macOS runtime is intentionally not committed")
        for name, digest in expected.items():
            with self.subTest(name=name):
                payload = (runtime_root / name).read_bytes()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)

    def test_staged_scrcpy_is_arm64_macho(self):
        executable = PROJECT_ROOT / "scrcpy-runtime-macos-arm64" / "scrcpy"
        if not executable.is_file():
            self.skipTest("The downloaded macOS runtime is intentionally not committed")
        header = executable.read_bytes()[:8]
        self.assertEqual(int.from_bytes(header[:4], "little"), 0xFEEDFACF)
        self.assertEqual(int.from_bytes(header[4:8], "little"), 0x0100000C)

    def test_supplied_logo_has_a_mac_icon_bundle(self):
        icon = PROJECT_ROOT / "assets" / "convrse-logo.icns"
        self.assertTrue(icon.is_file())
        self.assertGreater(icon.stat().st_size, 100_000)


if __name__ == "__main__":
    unittest.main()
