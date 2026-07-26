import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scrcpy_remote


class RuntimeToolResolutionTests(unittest.TestCase):
    @staticmethod
    def _tool_name(name):
        return name + (".exe" if os.name == "nt" else "")

    @staticmethod
    def _create_file(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return str(path)

    def test_frozen_bundle_runtime_has_highest_priority(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_root = root / "bundle"
            executable_root = root / "installed"
            tool_name = self._tool_name("scrcpy")
            bundled = self._create_file(
                bundle_root / "scrcpy-runtime" / tool_name)
            self._create_file(bundle_root / tool_name)
            self._create_file(executable_root / "scrcpy-runtime" / tool_name)
            self._create_file(executable_root / tool_name)

            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(
                        sys, "_MEIPASS", str(bundle_root), create=True), \
                    mock.patch.object(
                        scrcpy_remote, "app_dir", return_value=str(executable_root)):
                resolved = scrcpy_remote.find_tool("scrcpy")

            self.assertEqual(os.path.normcase(resolved), os.path.normcase(bundled))

    def test_frozen_root_and_external_runtime_are_compatible_fallbacks(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_root = root / "bundle"
            executable_root = root / "installed"
            tool_name = self._tool_name("adb")
            frozen_root_tool = self._create_file(bundle_root / tool_name)
            self._create_file(executable_root / "scrcpy-runtime" / tool_name)

            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(
                        sys, "_MEIPASS", str(bundle_root), create=True), \
                    mock.patch.object(
                        scrcpy_remote, "app_dir", return_value=str(executable_root)):
                resolved = scrcpy_remote.find_tool("adb")

            self.assertEqual(
                os.path.normcase(resolved), os.path.normcase(frozen_root_tool))

    def test_development_sidecar_runtime_precedes_app_root(self):
        with TemporaryDirectory() as temp_dir:
            executable_root = Path(temp_dir) / "source"
            tool_name = self._tool_name("scrcpy")
            sidecar = self._create_file(
                executable_root / "scrcpy-runtime" / tool_name)
            self._create_file(executable_root / tool_name)

            with mock.patch.object(sys, "frozen", False, create=True), \
                    mock.patch.object(
                        scrcpy_remote, "app_dir", return_value=str(executable_root)):
                resolved = scrcpy_remote.find_tool("scrcpy")

            self.assertEqual(os.path.normcase(resolved), os.path.normcase(sidecar))

    def test_path_name_is_returned_when_no_bundled_tool_exists(self):
        with TemporaryDirectory() as temp_dir:
            bundle_root = Path(temp_dir) / "bundle"
            executable_root = Path(temp_dir) / "installed"

            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(
                        sys, "_MEIPASS", str(bundle_root), create=True), \
                    mock.patch.object(
                        scrcpy_remote, "app_dir", return_value=str(executable_root)):
                self.assertEqual(scrcpy_remote.find_tool("scrcpy"), "scrcpy")


if __name__ == "__main__":
    unittest.main()
