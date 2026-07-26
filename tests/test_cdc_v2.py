import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

import cdc_v2
from cdc_ai_guard import (
    AI_DISPLAY_TOGGLES,
    PRESERVED_FIRMWARE_PROPERTIES,
    build_off_property_map,
)


class FakeProcess:
    """Small subprocess.Popen surface used by mirror lifecycle tests."""

    def __init__(self, running=True, returncode=0):
        self.running = running
        self.returncode = None if running else returncode
        self.stdout = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.running else self.returncode

    def terminate(self):
        self.terminated = True
        self.running = False
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.running = False
        self.returncode = -9


class CdcV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["cdc-v2-tests"])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.app.setStyleSheet(cdc_v2.V2_STYLESHEET)

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.settings_path = str(Path(self.temp_dir.name) / "cdc-v2-test.ini")

        def isolated_settings(*_args, **_kwargs):
            return QSettings(self.settings_path, QSettings.Format.IniFormat)

        self.settings_patch = mock.patch.object(
            cdc_v2, "QSettings", side_effect=isolated_settings
        )
        self.refresh_patch = mock.patch.object(
            cdc_v2.CdcV2Window, "refresh_devices", autospec=True
        )
        self.settings_patch.start()
        self.refresh_patch.start()
        self.windows = []

    def tearDown(self):
        for window in reversed(self.windows):
            window.ai_guard_timer.stop()
            window.scrcpy_proc = None
            for timer_id in list(window._timers):
                window.after_cancel(timer_id)
            window.close()
            window.deleteLater()
        self._drain_events()
        self.refresh_patch.stop()
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    def _window(self):
        window = cdc_v2.CdcV2Window()
        self.windows.append(window)
        window.showMaximized()
        self._drain_events()
        return window

    @classmethod
    def _drain_events(cls, cycles=4):
        for _ in range(cycles):
            cls.app.processEvents()

    @staticmethod
    def _argument_value(command, flag):
        index = command.index(flag)
        return command[index + 1]

    @staticmethod
    def _setprop_calls(adb_mock):
        return [
            item
            for item in adb_mock.call_args_list
            if len(item.args) >= 4
            and item.args[0] == "shell"
            and item.args[1] == "setprop"
        ]

    @staticmethod
    def _preferences_xml(*true_keys):
        true_keys = set(true_keys)
        entries = "\n".join(
            f'    <boolean name="{spec.preference_key}" '
            f'value="{str(spec.preference_key in true_keys).lower()}" />'
            for spec in AI_DISPLAY_TOGGLES
        )
        return (
            "<?xml version='1.0' encoding='utf-8'?>\n"
            f"<map>\n{entries}\n</map>\n"
        ).encode("utf-8")

    @staticmethod
    def _preferences_off_mapping():
        return {spec.preference_key: False for spec in AI_DISPLAY_TOGGLES}

    def test_each_preset_builds_the_documented_scrcpy_command(self):
        window = self._window()
        window.worker = lambda _callback: None
        window.after = lambda *_args, **_kwargs: None

        expected = {
            "Low": ("640", "750000", "20"),
            "Balanced": ("960", "1500000", "25"),
            "Normal": ("1280", "5000000", "30"),
            "Native": (None, "8000000", "30"),
            "Max": (None, "12000000", "60"),
        }

        with mock.patch.object(window, "require_serial", return_value="fake:17000"), \
                mock.patch.object(cdc_v2.subprocess, "Popen") as popen:
            window._guard_status_by_serial["fake:17000"] = "protected"
            for name, (max_size, bitrate, max_fps) in expected.items():
                with self.subTest(name=name):
                    process = FakeProcess()
                    popen.reset_mock()
                    popen.return_value = process
                    window.scrcpy_proc = None
                    window.select_preset(name, restart=False)

                    window.start_scrcpy()

                    command = popen.call_args.args[0]
                    if max_size is None:
                        self.assertNotIn("--max-size", command)
                    else:
                        self.assertEqual(
                            self._argument_value(command, "--max-size"), max_size
                        )
                    self.assertEqual(
                        self._argument_value(command, "--video-bit-rate"), bitrate
                    )
                    self.assertEqual(
                        self._argument_value(command, "--max-fps"), max_fps
                    )
                    self.assertIn("--print-fps", command)
                    self.assertEqual(
                        self._argument_value(command, "--video-codec"), "h264"
                    )
                    self.assertEqual(
                        self._argument_value(command, "--audio-source"), "output"
                    )
                    self.assertEqual(
                        self._argument_value(command, "--audio-codec"), "opus"
                    )
                    self.assertEqual(
                        self._argument_value(command, "--audio-bit-rate"), "128K"
                    )
                    self.assertEqual(
                        self._argument_value(command, "--audio-buffer"), "200"
                    )
                    self.assertNotIn("--audio-dup", command)
                    self.assertNotIn("--require-audio", command)
                    self.assertNotIn("--audio-output-buffer", command)
                    self.assertEqual(
                        self._argument_value(command, "-s"), "fake:17000"
                    )

        self.assertEqual(cdc_v2.DEFAULT_PRESET, "Balanced")

    def test_minimal_header_and_two_category_navigation(self):
        window = self._window()

        self.assertEqual(window.windowTitle(), cdc_v2.APP_NAME)
        self.assertEqual(
            [button.text() for button in window.category_buttons],
            ["Remote", "Device"],
        )
        self.assertEqual(window.category_stack.count(), 2)
        self.assertFalse(hasattr(window, "stage_toolbar"))
        remote_page_text = " ".join(
            label.text()
            for label in window.category_stack.widget(0).findChildren(cdc_v2.QLabel)
        )
        self.assertIn("App recovery", remote_page_text)

        full_screen_buttons = [
            button
            for button in window.findChildren(QPushButton)
            if button.text() == "Full screen"
        ]
        self.assertEqual(full_screen_buttons, [window.header_focus_button])
        self.assertFalse(hasattr(window, "focus_overlay"))
        self.assertFalse(any(
            button.text() == "Exit focus"
            for button in window.findChildren(QPushButton)
        ))
        visible_text = " ".join(
            label.text() for label in window.findChildren(cdc_v2.QLabel)
            if label.isVisible()
        )
        self.assertNotIn(" V2", visible_text)
        self.assertNotIn(
            "V2", [label.text() for label in window.findChildren(cdc_v2.QLabel)]
        )

    def test_alt_toggles_the_hidden_menu_bar(self):
        window = self._window()
        self.assertFalse(window.menuBar().isVisible())

        QTest.keyClick(window, Qt.Key.Key_Alt)
        self._drain_events()
        self.assertTrue(window.menuBar().isVisible())

        QTest.keyClick(window, Qt.Key.Key_Escape)
        self._drain_events()
        self.assertFalse(window.menuBar().isVisible())

        QTest.keyClick(window, Qt.Key.Key_Alt)
        self._drain_events()
        self.assertTrue(window.menuBar().isVisible())
        QTest.mouseClick(window.mirror_frame, Qt.MouseButton.LeftButton)
        self._drain_events()
        self.assertFalse(window.menuBar().isVisible())

        QTest.keyClick(window, Qt.Key.Key_Alt)
        self._drain_events()
        self.assertTrue(window.menuBar().isVisible())

        refresh_action = next(
            action for action in window.findChildren(cdc_v2.QAction)
            if action.text() == "Refresh device"
        )
        refresh_action.trigger()
        self._drain_events()
        self.assertFalse(window.menuBar().isVisible())

        QTest.keyClick(window, Qt.Key.Key_Alt)
        self._drain_events()
        self.assertTrue(window.menuBar().isVisible())
        QTest.keyClick(window, Qt.Key.Key_Alt)
        self._drain_events()
        self.assertFalse(window.menuBar().isVisible())

    def test_preset_combo_is_exact_and_persists(self):
        window = self._window()
        expected = [
            "Low · 640 × 360 · 20 FPS · 0.75 Mbps · H.264",
            "Balanced · 960 × 540 · 25 FPS · 1.5 Mbps · H.264",
            "Normal · 1280 × 720 · 30 FPS · 5 Mbps · H.264",
            "Native · Device native · 30 FPS · 8 Mbps · H.264",
            "Max · Device native · 60 FPS · 12 Mbps · H.264",
            "Custom…",
        ]
        self.assertEqual(
            [
                window.profile_combo.itemText(index)
                for index in range(window.profile_combo.count())
            ],
            expected,
        )
        self.assertEqual(window.profile_combo.currentData(), "Balanced")

        normal_index = window.profile_combo.findData("Normal")
        window.profile_combo.setCurrentIndex(normal_index)
        self._drain_events()
        self.assertEqual(window.selected_preset, "Normal")
        window._save_layout()

        restored = self._window()
        self.assertEqual(restored.selected_preset, "Normal")
        self.assertEqual(restored.profile_combo.currentData(), "Normal")

    def test_profile_combo_changes_only_after_an_explicit_selection(self):
        window = self._window()
        original_index = window.profile_combo.currentIndex()
        wheel = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )

        window.profile_combo.wheelEvent(wheel)

        self.assertEqual(window.profile_combo.currentIndex(), original_index)
        self.assertFalse(wheel.isAccepted())
        window.profile_combo.setCurrentIndex(
            window.profile_combo.findData("Normal"))
        self.assertEqual(window.selected_preset, "Normal")

    def test_explicit_profile_selection_remains_available_during_mirroring(self):
        window = self._window()
        window.scrcpy_proc = FakeProcess()
        window._mirror_state = "running"

        with mock.patch.object(window, "_terminate_scrcpy") as terminate:
            window.select_preset("Normal")

        self.assertEqual(window.selected_preset, "Normal")
        self.assertTrue(window._pending_restart)
        self.assertEqual(window._mirror_state, "restarting")
        terminate.assert_called_once_with()

    def test_custom_controls_are_hidden_until_selected_and_apply_once(self):
        window = self._window()
        window.worker = lambda _callback: None
        window.after = lambda *_args, **_kwargs: None
        self.assertTrue(window.custom_profile_panel.isHidden())

        custom_index = window.profile_combo.findData(cdc_v2.CUSTOM_PRESET)
        window.profile_combo.setCurrentIndex(custom_index)
        self._drain_events()
        self.assertFalse(window.custom_profile_panel.isHidden())
        self.assertEqual(window.selected_preset, "Balanced")

        with mock.patch.object(window, "_terminate_scrcpy") as terminate:
            window.custom_resolution_combo.setCurrentIndex(
                window.custom_resolution_combo.findData(960))
            window.custom_fps_combo.setCurrentIndex(
                window.custom_fps_combo.findData(60))
            window.custom_bitrate_spin.setValue(4.0)
            terminate.assert_not_called()

            window.scrcpy_proc = FakeProcess()
            window._mirror_state = "running"
            window._apply_custom_profile()
            terminate.assert_called_once_with()

        self.assertEqual(window.selected_preset, cdc_v2.CUSTOM_PRESET)
        self.assertTrue(window._pending_restart)
        config = window._resolve_stream_config("fake:17000")
        self.assertEqual(config.max_size, 960)
        self.assertEqual(config.max_fps, 60)
        self.assertEqual(config.bitrate_bps, 4_000_000)

        window.scrcpy_proc = None
        window.select_preset("Normal", restart=False)
        self._drain_events()
        self.assertTrue(window.custom_profile_panel.isHidden())

    def test_connection_actions_and_diagnostic_button_are_compact(self):
        window = self._window()

        def assert_matching_connection_actions():
            self._drain_events()
            tunnel_geometry = window.tunnel_button_widget.geometry()
            mirror_geometry = window.start_button_widget.geometry()
            self.assertEqual(tunnel_geometry.top(), mirror_geometry.top())
            self.assertEqual(tunnel_geometry.size(), mirror_geometry.size())
            self.assertEqual(tunnel_geometry.height(), 40)
            self.assertEqual(
                window.tunnel_button_widget.objectName(), "ConnectionAction"
            )
            self.assertEqual(
                window.start_button_widget.objectName(), "ConnectionAction"
            )

        window._set_tunnel_state("Disconnected", "idle")
        window._set_mirror_state("stopped")
        assert_matching_connection_actions()
        self.assertEqual(window.start_button_widget.text(), "Start mirror")
        self.assertFalse(window.start_button_widget.isEnabled())
        self.assertIn("Connect a device", window.start_button_widget.toolTip())

        with mock.patch.object(window, "active_serial", return_value="fake:17000"):
            window._set_tunnel_state("Active", "online")
            window._set_mirror_state("running")
            assert_matching_connection_actions()
            self.assertEqual(window.tunnel_button_widget.text(), "Disconnect")
            self.assertEqual(window.start_button_widget.text(), "Stop mirror")
            self.assertEqual(window.start_button_widget.property("role"), "danger")
            window._set_mirror_state("ready")
            self.assertEqual(window.start_button_widget.text(), "Start mirror")
            self.assertEqual(window.start_button_widget.property("role"), "primary")

        window.capture_btn.configure(text="Stop session · 29:59")
        self.assertEqual(
            window.capture_button_widget.text(), "Stop diagnostic session"
        )
        self.assertEqual(
            window.capture_button_widget.height(), cdc_v2.CONTROL_HEIGHT
        )

        for button in window.sidebar.findChildren(QPushButton):
            expected_height = (
                cdc_v2.PRIMARY_ACTION_HEIGHT
                if button.objectName() == "ConnectionAction"
                else cdc_v2.CONTROL_HEIGHT
            )
            with self.subTest(button=button.text()):
                self.assertEqual(button.height(), expected_height)
                self.assertEqual(
                    button.sizePolicy().horizontalPolicy(),
                    cdc_v2.QSizePolicy.Policy.Expanding,
                )

        self.assertEqual(
            window.sidebar_button.width(), cdc_v2.APP_BAR_CONTROL_HEIGHT
        )
        self.assertEqual(
            window.sidebar_button.height(), cdc_v2.APP_BAR_CONTROL_HEIGHT
        )
        self.assertEqual(
            window.header_focus_button.height(), cdc_v2.APP_BAR_CONTROL_HEIGHT
        )
        self.assertEqual(
            window.device_pill.height(), cdc_v2.APP_BAR_CONTROL_HEIGHT
        )
        self.assertEqual(
            window.tunnel_label.height(), cdc_v2.APP_BAR_CONTROL_HEIGHT
        )

    def test_connection_actions_remain_equal_with_an_odd_row_width(self):
        row = cdc_v2.EqualActionRow(spacing=6)
        left = QPushButton("Connect")
        right = QPushButton("Start mirror")
        row.set_actions(left, right)
        row.resize(317, cdc_v2.PRIMARY_ACTION_HEIGHT)
        row._layout_actions()

        self.assertEqual(left.size(), right.size())
        self.assertEqual(left.width(), 155)
        self.assertEqual(right.x() - left.geometry().right() - 1, 6)

    def test_existing_local_adb_transport_is_shown_as_active_and_disconnectable(self):
        window = self._window()
        serial = "127.0.0.1:17000"
        window.port_edit.setText("17000")
        window._ai_guard_last_serial = serial
        window._guard_status_by_serial[serial] = "protected"

        window._device_changed(serial)

        self.assertEqual(window.tunnel_label.text(), "Active on 17000")
        self.assertEqual(window.tunnel_label.property("tone"), "online")
        self.assertEqual(window.tunnel_button_widget.text(), "Disconnect")
        self.assertTrue(window._external_tunnel)
        with mock.patch.object(window, "disconnect_tunnel") as disconnect:
            window._toggle_tunnel()
        disconnect.assert_called_once_with()

        window._set_tunnel_state("Disconnected", "idle")
        window._device_changed("usb-device")
        self.assertEqual(window.tunnel_label.text(), "Disconnected")
        self.assertFalse(window._external_tunnel)

    def test_aspect_host_centers_content_without_cropping(self):
        host = cdc_v2.AspectMirrorHost()
        container = QWidget(host)
        host.foreign_container = container

        for host_size, ratio in (((1000, 500), 16 / 9), ((500, 900), 9 / 16)):
            with self.subTest(host_size=host_size, ratio=ratio):
                host.resize(*host_size)
                host.set_aspect(ratio)
                host._layout_content()

                available = host.contentsRect().adjusted(2, 2, -2, -2)
                expected_width = available.width()
                expected_height = int(expected_width / ratio)
                if expected_height > available.height():
                    expected_height = available.height()
                    expected_width = int(expected_height * ratio)
                expected_x = available.x() + (
                    available.width() - expected_width
                ) // 2
                expected_y = available.y() + (
                    available.height() - expected_height
                ) // 2

                geometry = container.geometry()
                self.assertEqual(
                    geometry.getRect(),
                    (expected_x, expected_y, expected_width, expected_height),
                )
                self.assertTrue(available.contains(geometry))

        host.foreign_container = None
        container.deleteLater()
        host.deleteLater()

    def test_stale_process_generation_cannot_update_or_adopt(self):
        window = self._window()
        stale = FakeProcess()
        current = FakeProcess()
        window.scrcpy_proc = current
        window._mirror_generation = 8
        window._scrcpy_resolution = None
        window._scrcpy_fps = None

        with mock.patch.object(window.mirror_frame, "set_aspect") as set_aspect, \
                mock.patch.object(window.mirror_frame, "adopt_window") as adopt, \
                mock.patch.object(window, "after") as after:
            window._apply_stream_metrics(stale, 7, 99.0, 640, 360)
            window._adopt_scrcpy_window_for_process(stale, 1234, 7)
            window._poll_scrcpy(stale, 7)

            self.assertIsNone(window._scrcpy_resolution)
            self.assertIsNone(window._scrcpy_fps)
            set_aspect.assert_not_called()
            adopt.assert_not_called()
            after.assert_not_called()

            window._apply_stream_metrics(current, 8, 29.5, 800, 600)
            self.assertEqual(window._scrcpy_resolution, "800 × 600")
            self.assertEqual(window._scrcpy_fps, 29.5)
            self.assertAlmostEqual(window.mirror_aspect, 4 / 3)
            set_aspect.assert_called_once_with(4 / 3)

        window.scrcpy_proc = None

    def test_metric_label_distinguishes_targets_from_measurements(self):
        window = self._window()
        window.select_preset("Balanced", restart=False)

        window._scrcpy_resolution = None
        window._scrcpy_fps = None
        window._update_stream_metrics_label()
        target_only = window.stream_metrics_label.text()
        self.assertTrue(target_only.startswith("Target "))
        self.assertIn("960 × 540", target_only)
        self.assertNotIn("Actual", target_only)

        window._scrcpy_fps = 24.5
        window._update_stream_metrics_label()
        fps_only = window.stream_metrics_label.text()
        self.assertIn("Target 960 × 540", fps_only)
        self.assertIn("Actual 24.5 FPS", fps_only)
        self.assertNotIn("Actual 960 × 540", fps_only)

        window._scrcpy_resolution = "956 × 540"
        window._scrcpy_fps = None
        window._update_stream_metrics_label()
        size_only = window.stream_metrics_label.text()
        self.assertIn("Target ≤25 FPS", size_only)
        self.assertIn("Actual 956 × 540", size_only)

        window._scrcpy_fps = 23.75
        window._update_stream_metrics_label()
        fully_measured = window.stream_metrics_label.text()
        self.assertEqual(fully_measured, "Actual 956 × 540 · 23.8 FPS")

    def test_native_max_and_encoder_are_resolved_from_device_capabilities(self):
        window = self._window()
        serial = "rk-device:17000"
        window._stream_capabilities[serial] = cdc_v2.DeviceStreamCapabilities(
            width=1920,
            height=1080,
            refresh_hz=59.94,
            sdk=34,
            h264_encoder="c2.rk.avc.encoder",
        )

        window.select_preset("Native", restart=False)
        native = window._resolve_stream_config(serial)
        self.assertEqual(native.resolution, "1920 × 1080")
        self.assertEqual(native.max_size, 0)
        self.assertEqual(native.max_fps, 30)
        self.assertEqual(native.h264_encoder, "c2.rk.avc.encoder")

        window.select_preset("Max", restart=False)
        maximum = window._resolve_stream_config(serial)
        self.assertEqual(maximum.resolution, "1920 × 1080")
        self.assertEqual(maximum.max_fps, 60)
        command = window._build_scrcpy_command(serial, "CDC test", maximum)
        self.assertEqual(
            self._argument_value(command, "--video-encoder"),
            "c2.rk.avc.encoder",
        )
        self.assertNotIn("--max-size", command)

        window._stream_encoder_bypass.add(serial)
        fallback = window._resolve_stream_config(serial)
        self.assertIsNone(fallback.h264_encoder)

    def test_stream_capability_parsers_reject_alias_and_software_encoders(self):
        output = """
            --video-codec=h264 --video-encoder='c2.android.avc.encoder' (sw)
            --video-codec=h264 --video-encoder='OMX.rk.video_encoder.avc' (hw) (alias)
            --video-codec=h264 --video-encoder='c2.rk.avc.encoder' (hw)
        """
        self.assertEqual(
            cdc_v2.CdcV2Window._parse_h264_hardware_encoder(output),
            "c2.rk.avc.encoder",
        )
        self.assertEqual(
            cdc_v2.CdcV2Window._parse_wm_size(
                "Physical size: 1920x1080\nOverride size: 1280x720"),
            (1280, 720),
        )
        self.assertEqual(
            cdc_v2.CdcV2Window._parse_refresh_rate(
                "mActiveSfDisplayMode=DisplayMode{fps=59.94}"),
            59.94,
        )

    def test_focus_mode_restores_expanded_and_collapsed_layouts(self):
        window = self._window()
        window._set_sidebar_visible(True, persist=False)
        window._set_console_visible(True, persist=False)
        window.main_splitter.setSizes([326, 900])
        window.workspace_splitter.setSizes([620, 180])
        self._drain_events()

        before_main = window.main_splitter.sizes()
        before_workspace = window.workspace_splitter.sizes()
        window.set_focus_mode(True)
        self._drain_events()
        self.assertTrue(window._focus_mode)
        self.assertTrue(window.isFullScreen())
        self.assertFalse(window.sidebar.isVisible())
        self.assertFalse(window.console.isVisible())
        self.assertFalse(hasattr(window, "focus_overlay"))

        QTest.keyClick(window, Qt.Key.Key_Escape)
        self._drain_events()
        self.assertFalse(window._focus_mode)
        self.assertTrue(window.isMaximized())
        self.assertTrue(window.sidebar.isVisible())
        self.assertFalse(window._console_collapsed)
        self.assertTrue(window.console.isVisible())
        self.assertEqual(window.main_splitter.sizes(), before_main)
        self.assertEqual(window.workspace_splitter.sizes(), before_workspace)

        window._set_sidebar_visible(False, persist=False)
        window._set_console_visible(False, persist=False)
        self._drain_events()
        window.set_focus_mode(True)
        self._drain_events()
        window.exit_focus_mode()
        self._drain_events()

        self.assertFalse(window.sidebar.isVisible())
        self.assertTrue(window._console_collapsed)
        self.assertTrue(window.console.isVisible())
        self.assertFalse(window.console.output.isVisible())
        self.assertEqual(window.console.height(), 22)

    def test_header_f11_and_double_click_keep_fullscreen_accessible(self):
        window = self._window()
        self.assertEqual(window.focus_action.shortcut().toString(), "F11")

        window.header_focus_button.click()
        self._drain_events()
        self.assertTrue(window._focus_mode)
        window.focus_escape_shortcut.activated.emit()
        self._drain_events()
        self.assertFalse(window._focus_mode)

        window.focus_action.trigger()
        self._drain_events()
        self.assertTrue(window._focus_mode)
        window.focus_escape_shortcut.activated.emit()
        self._drain_events()
        self.assertFalse(window._focus_mode)

        window.mirror_frame.doubleClicked.emit()
        self._drain_events()
        self.assertTrue(window._focus_mode)
        window.focus_escape_shortcut.activated.emit()
        self._drain_events()
        self.assertFalse(window._focus_mode)

    def test_collapsed_visibility_persists_in_isolated_settings(self):
        first = self._window()
        first._set_sidebar_visible(False)
        first._set_console_visible(False)
        first._save_layout()
        self._drain_events()

        second = self._window()
        self.assertFalse(second.sidebar.isVisible())
        self.assertTrue(second._console_collapsed)
        self.assertFalse(second.console.output.isVisible())

    def test_command_log_defaults_to_thin_strip_without_action_buttons(self):
        window = self._window()
        self.assertTrue(window._console_collapsed)
        self.assertEqual(window.console.height(), 22)
        self.assertFalse(window.console.output.isVisible())

        console_button_texts = {
            button.text() for button in window.console.findChildren(QPushButton)
            if not button.isHidden()
        }
        self.assertEqual(console_button_texts, {"⌃ Command log"})
        self.assertTrue(
            {"Copy", "Save", "Clear"}.isdisjoint(console_button_texts)
        )

        window._set_console_visible(True, persist=False)
        window.console.output.setPlainText("selectable command output")
        window.console.output.selectAll()
        window.console.output.setFocus()
        QApplication.clipboard().clear()
        QTest.keyClick(
            window.console.output,
            Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier,
        )
        self._drain_events()
        self.assertEqual(
            QApplication.clipboard().text(), "selectable command output"
        )

    def test_ai_control_rows_are_collapsed_and_redundant_actions_are_absent(self):
        window = self._window()
        window._show_category(1)
        self._drain_events()
        self.assertFalse(window.ai_controls_panel.isVisible())
        self.assertEqual(window.ai_controls_button.text(), "7 controls  ▸")

        window.ai_controls_button.click()
        self._drain_events()
        self.assertTrue(window.ai_controls_panel.isVisible())
        self.assertEqual(window.ai_controls_button.text(), "7 controls  ▾")

        device_page_text = " ".join(
            button.text()
            for button in window.category_stack.widget(1).findChildren(QPushButton)
        )
        self.assertNotIn("Open display", device_page_text)
        self.assertNotIn("Enforce", device_page_text)

    def test_new_device_invokes_full_automatic_guard_once(self):
        window = self._window()
        window._ai_guard_last_serial = None

        with mock.patch.object(window, "enforce_ai_pq_off") as enforce:
            window._device_changed("RK3576-USB-004")

            enforce.assert_called_once()
            self.assertTrue(enforce.call_args.kwargs["silent"])
            self.assertTrue(enforce.call_args.kwargs["apply_all"])
            self.assertTrue(callable(enforce.call_args.kwargs["on_complete"]))
            self.assertEqual(window._ai_guard_last_serial, "RK3576-USB-004")

            # Repeated selection signals for the same device must not launch a
            # second connection baseline.
            window._device_changed("RK3576-USB-004")
            self.assertEqual(enforce.call_count, 1)

            # Any other opaque serial gets its own automatic full baseline.
            window._device_changed("display-lab-b.local:5555")
            self.assertEqual(enforce.call_count, 2)
            self.assertTrue(enforce.call_args.kwargs["silent"])
            self.assertTrue(enforce.call_args.kwargs["apply_all"])
            self.assertTrue(callable(enforce.call_args.kwargs["on_complete"]))

    def test_generic_firmware_skips_root_and_setprop_and_reports_unsupported(self):
        window = self._window()
        serial = "generic-android-usb"
        generic_snapshot = {"ro.board.platform": "generic"}

        with mock.patch.object(window, "active_serial", return_value=serial), \
                mock.patch.object(
                    window,
                    "_read_ai_pq_state",
                    return_value=((0, ""), generic_snapshot),
                ), \
                mock.patch.object(
                    window, "worker", side_effect=lambda callback: callback()
                ), \
                mock.patch.object(
                    window,
                    "ui",
                    side_effect=lambda callback, *args: callback(*args),
                ), \
                mock.patch.object(window, "_root_access_mode") as root_mode, \
                mock.patch.object(cdc_v2.legacy, "adb_command") as adb_command:
            window.enforce_ai_pq_off(silent=True, apply_all=True)

        root_mode.assert_not_called()
        adb_command.assert_not_called()
        self.assertEqual(
            window.ai_pq_status_label.text(),
            "Not applicable · supported display controls were not found",
        )
        self.assertTrue(
            all(label.text() == "N/A" for label in window.ai_toggle_labels.values())
        )

    def test_cached_on_setting_can_never_be_reported_as_protected(self):
        window = self._window()
        serial = "cached-on-rk"
        preferences = self._preferences_off_mapping()
        preferences["ai_dc"] = True

        with mock.patch.object(window, "require_serial", return_value=serial), \
                mock.patch.object(
                    window,
                    "_read_ai_pq_state",
                    return_value=((0, ""), build_off_property_map()),
                ), \
                mock.patch.object(window, "_root_access_mode", return_value="adb"), \
                mock.patch.object(
                    window,
                    "_read_ai_preference_booleans",
                    return_value=(cdc_v2.AI_SETTINGS_PREFS_PATH, preferences),
                ), \
                mock.patch.object(
                    window, "worker", side_effect=lambda callback: callback()
                ), \
                mock.patch.object(
                    window,
                    "ui",
                    side_effect=lambda callback, *args: callback(*args),
                ):
            window.verify_ai_pq_state()

        self.assertIn("Settings cache", window.ai_pq_status_label.text())
        self.assertNotIn("protected", window.ai_pq_status_label.text().casefold())
        self.assertEqual(window.ai_toggle_labels["AI-DC"].text(), "ON · cached")
        self.assertEqual(window._guard_status_by_serial[serial], "failed")

    def test_mirror_is_gated_until_initial_guard_finishes(self):
        window = self._window()
        serial = "guard-first-rk"
        with mock.patch.object(window, "enforce_ai_pq_off") as enforce, \
                mock.patch.object(window, "_start_stream_capability_probe") as probe, \
                mock.patch.object(
                    window, "_current_device_serial", return_value=serial
                ), \
                mock.patch.object(window, "active_serial", return_value=serial):
            window._device_changed(serial)
            self.assertEqual(window._mirror_state, "protecting")
            self.assertFalse(window.start_button_widget.isEnabled())
            completion = enforce.call_args.kwargs["on_complete"]

            window._guard_status_by_serial[serial] = "protected"
            completion(True)
            probe.assert_called_once_with(serial)

            capabilities = cdc_v2.DeviceStreamCapabilities(
                width=1920,
                height=1080,
                refresh_hz=60,
                h264_encoder="c2.rk.avc.encoder",
            )
            window._apply_stream_capabilities(
                serial, window._stream_probe_generation, capabilities)
            self.assertEqual(window._mirror_state, "ready")
            self.assertTrue(window.start_button_widget.isEnabled())

    def test_root_failure_exposes_actionable_reason(self):
        window = self._window()
        serial = "non-root-device"
        responses = [
            (0, "uid=2000(shell) gid=2000(shell)", 0.1),
            (1, "su: permission denied", 0.1),
            (1, "adbd cannot run as root in production builds", 0.1),
        ]
        with mock.patch.object(
                cdc_v2.legacy, "adb_command", side_effect=responses):
            self.assertIsNone(window._root_access_mode(serial))
        self.assertIn(
            "not rooted",
            window._root_failure_reason_by_serial[serial].casefold(),
        )

    def test_preferences_path_uses_the_current_android_user(self):
        window = self._window()
        serial = "multi-user-rk"
        with mock.patch.object(window, "_current_android_user", return_value=10), \
                mock.patch.object(
                    window, "_root_path_exists", side_effect=[True]
                ):
            path = window._resolve_ai_preferences_path(serial, "adb")
        self.assertEqual(
            path,
            "/data/user_de/10/com.android.tv.settings/shared_prefs/"
            "com.android.tv.settings_preferences.xml",
        )

    def test_full_connection_baseline_writes_every_supported_property(self):
        window = self._window()
        serial = "10.42.0.18:5555"
        snapshot = build_off_property_map()

        with mock.patch.object(window, "active_serial", return_value=serial), \
                mock.patch.object(
                    window,
                    "_read_ai_pq_state",
                    side_effect=[((0, ""), dict(snapshot))] * 4,
                ), \
                mock.patch.object(
                    window, "worker", side_effect=lambda callback: callback()
                ), \
                mock.patch.object(
                    window,
                    "ui",
                    side_effect=lambda callback, *args: callback(*args),
                ), \
                mock.patch.object(window, "_root_access_mode", return_value="adb"), \
                mock.patch.object(
                    window, "_synchronize_ai_preferences_off", return_value=()
                ) as sync_preferences, \
                mock.patch.object(
                    window,
                    "_read_ai_preference_booleans",
                    return_value=(
                        cdc_v2.AI_SETTINGS_PREFS_PATH,
                        self._preferences_off_mapping(),
                    ),
                ), \
                mock.patch.object(
                    cdc_v2.legacy, "adb_command", return_value=(0, "")
                ) as adb_command, \
                mock.patch.object(cdc_v2.time, "sleep"):
            window.enforce_ai_pq_off(silent=True, apply_all=True)

        setprop_calls = self._setprop_calls(adb_command)
        written = {item.args[2]: item.args[3] for item in setprop_calls}
        self.assertEqual(len(setprop_calls), len(snapshot))
        self.assertEqual(written, snapshot)
        sync_preferences.assert_called_once_with(
            serial, "adb", refresh_for_runtime_change=True)
        self.assertTrue(
            window.ai_pq_status_label.text().startswith("Automatic")
        )
        self.assertIn("7/7 protected", window.ai_pq_status_label.text())

    def test_watchdog_writes_corrections_only(self):
        window = self._window()
        serial = "watchdog-rk3576"
        drifted_snapshot = build_off_property_map()
        drifted_snapshot["persist.vendor.sculptor.mode"] = "1"
        verified_snapshot = build_off_property_map()

        with mock.patch.object(window, "active_serial", return_value=serial), \
                mock.patch.object(
                    window,
                    "_read_ai_pq_state",
                    side_effect=[
                        ((0, ""), drifted_snapshot),
                        ((0, ""), verified_snapshot),
                        ((0, ""), verified_snapshot),
                        ((0, ""), verified_snapshot),
                    ],
                ), \
                mock.patch.object(
                    window, "worker", side_effect=lambda callback: callback()
                ), \
                mock.patch.object(
                    window,
                    "ui",
                    side_effect=lambda callback, *args: callback(*args),
                ), \
                mock.patch.object(window, "_root_access_mode", return_value="adb"), \
                mock.patch.object(
                    window, "_synchronize_ai_preferences_off", return_value=()
                ) as sync_preferences, \
                mock.patch.object(
                    window,
                    "_read_ai_preference_booleans",
                    return_value=(
                        cdc_v2.AI_SETTINGS_PREFS_PATH,
                        self._preferences_off_mapping(),
                    ),
                ), \
                mock.patch.object(
                    cdc_v2.legacy, "adb_command", return_value=(0, "")
                ) as adb_command, \
                mock.patch.object(cdc_v2.time, "sleep"):
            window._ai_pq_watchdog()

        setprop_calls = self._setprop_calls(adb_command)
        self.assertEqual(len(setprop_calls), 1)
        self.assertEqual(
            setprop_calls[0].args,
            ("shell", "setprop", "persist.vendor.sculptor.mode", "0"),
        )
        sync_preferences.assert_called_once_with(
            serial, "adb", refresh_for_runtime_change=True)

    def test_partial_watchdog_syncs_cache_without_runtime_corrections(self):
        window = self._window()
        serial = "partial-rk-display"
        partial_snapshot = {"persist.vendor.sculptor.mode": "0"}

        with mock.patch.object(window, "active_serial", return_value=serial), \
                mock.patch.object(
                    window,
                    "_read_ai_pq_state",
                    return_value=((0, ""), partial_snapshot),
                ), \
                mock.patch.object(
                    window, "worker", side_effect=lambda callback: callback()
                ), \
                mock.patch.object(
                    window,
                    "ui",
                    side_effect=lambda callback, *args: callback(*args),
                ), \
                mock.patch.object(window, "_root_access_mode", return_value="adb"), \
                mock.patch.object(
                    window,
                    "_synchronize_ai_preferences_off",
                    return_value=("ai_visionpq",),
                ) as sync_preferences, \
                mock.patch.object(
                    window,
                    "_read_ai_preference_booleans",
                    return_value=(
                        cdc_v2.AI_SETTINGS_PREFS_PATH,
                        self._preferences_off_mapping(),
                    ),
                ), \
                mock.patch.object(cdc_v2.legacy, "adb_command") as adb_command:
            window._ai_pq_watchdog()

        self.assertEqual(self._setprop_calls(adb_command), [])
        sync_preferences.assert_called_once_with(
            serial, "adb", refresh_for_runtime_change=False)
        self.assertIn("supported controls protected", window.ai_pq_status_label.text())

    def test_preference_sync_force_stops_and_reopens_foreground_settings(self):
        window = self._window()
        serial = "foreground-settings-rk"
        source_xml = self._preferences_xml("ai_dc", "ai_sr")
        latest_source_xml = source_xml.replace(
            b"</map>",
            b'    <string name="unrelated_setting">newer value</string>\n</map>',
        )
        verified_xml = cdc_v2.sync_ai_display_preferences_off(
            latest_source_xml).xml_data
        events = []

        def process_ids(_serial):
            events.append("inspect-pid")
            return ("4821",) if events.count("inspect-pid") == 1 else ()

        def write_preferences(*_args):
            events.append("write")

        def navigate(_serial):
            events.append("navigate")

        with mock.patch.object(
                    window,
                    "_resolve_ai_preferences_path",
                    return_value=cdc_v2.AI_SETTINGS_PREFS_PATH,
                ), \
                mock.patch.object(
                    window, "_root_path_exists", side_effect=[False, False]
                ) as backup_exists, \
                mock.patch.object(
                    window,
                    "_read_root_file_bytes",
                    side_effect=[source_xml, latest_source_xml, verified_xml],
                ), \
                mock.patch.object(
                    window, "_settings_process_ids", side_effect=process_ids
                ), \
                mock.patch.object(
                    window, "_settings_is_foreground", return_value=True
                ), \
                mock.patch.object(
                    window,
                    "_write_root_preferences_atomically",
                    side_effect=write_preferences,
                ) as atomic_write, \
                mock.patch.object(
                    window, "_navigate_settings_display", side_effect=navigate
                ) as navigate_settings, \
                mock.patch.object(
                    cdc_v2.legacy, "adb_command", return_value=(0, "")
                ) as adb_command, \
                mock.patch.object(cdc_v2.time, "sleep"):
            changed_keys = window._synchronize_ai_preferences_off(serial, "adb")

        self.assertEqual(changed_keys, ("ai_dc", "ai_sr"))
        self.assertEqual(backup_exists.call_count, 2)
        atomic_write.assert_called_once_with(
            serial, "adb", verified_xml, cdc_v2.AI_SETTINGS_PREFS_PATH)
        navigate_settings.assert_called_once_with(serial)
        self.assertLess(events.index("write"), events.index("navigate"))
        self.assertIn(
            mock.call(
                "shell", "am", "force-stop", cdc_v2.AI_SETTINGS_PACKAGE,
                serial=serial, timeout=20, telemetry=False,
            ),
            adb_command.call_args_list,
        )

    def test_preference_sync_already_false_performs_no_write(self):
        window = self._window()
        serial = "already-off-rk"

        with mock.patch.object(
                    window,
                    "_resolve_ai_preferences_path",
                    return_value=cdc_v2.AI_SETTINGS_PREFS_PATH,
                ), \
                mock.patch.object(
                    window, "_root_path_exists", return_value=False
                ), \
                mock.patch.object(
                    window,
                    "_read_root_file_bytes",
                    return_value=self._preferences_xml(),
                ), \
                mock.patch.object(window, "_settings_process_ids") as process_ids, \
                mock.patch.object(window, "_settings_is_foreground") as foreground, \
                mock.patch.object(
                    window, "_write_root_preferences_atomically"
                ) as atomic_write, \
                mock.patch.object(
                    window, "_navigate_settings_display"
                ) as navigate_settings, \
                mock.patch.object(window, "logline") as logline:
            changed_keys = window._synchronize_ai_preferences_off(serial, "adb")

        self.assertEqual(changed_keys, ())
        process_ids.assert_not_called()
        foreground.assert_not_called()
        atomic_write.assert_not_called()
        navigate_settings.assert_not_called()
        logline.assert_not_called()

    def test_runtime_correction_refreshes_foreground_ui_without_cache_write(self):
        window = self._window()
        serial = "foreground-runtime-refresh-rk"
        all_off_xml = self._preferences_xml()
        process_checks = iter((("4821",), ()))

        with mock.patch.object(
                    window,
                    "_resolve_ai_preferences_path",
                    return_value=cdc_v2.AI_SETTINGS_PREFS_PATH,
                ), \
                mock.patch.object(
                    window, "_root_path_exists", side_effect=[False, False]
                ), \
                mock.patch.object(
                    window,
                    "_read_root_file_bytes",
                    side_effect=[all_off_xml, all_off_xml, all_off_xml],
                ), \
                mock.patch.object(
                    window,
                    "_settings_process_ids",
                    side_effect=lambda _serial: next(process_checks),
                ), \
                mock.patch.object(
                    window, "_settings_is_foreground", return_value=True
                ), \
                mock.patch.object(
                    window, "_write_root_preferences_atomically"
                ) as atomic_write, \
                mock.patch.object(
                    window, "_navigate_settings_display"
                ) as navigate_settings, \
                mock.patch.object(
                    cdc_v2.legacy, "adb_command", return_value=(0, "")
                ), \
                mock.patch.object(cdc_v2.time, "sleep"):
            changed_keys = window._synchronize_ai_preferences_off(
                serial,
                "adb",
                refresh_for_runtime_change=True,
            )

        self.assertEqual(changed_keys, ())
        atomic_write.assert_not_called()
        navigate_settings.assert_called_once_with(serial)

    def test_preference_sync_rejects_non_exact_readback(self):
        window = self._window()
        serial = "non-exact-cache-rk"
        source_xml = self._preferences_xml("ai_dc")
        expected_xml = cdc_v2.sync_ai_display_preferences_off(source_xml).xml_data
        non_exact_xml = expected_xml.replace(
            b"</map>", b'    <string name="unrelated">lost</string>\n</map>')

        with mock.patch.object(
                    window,
                    "_resolve_ai_preferences_path",
                    return_value=cdc_v2.AI_SETTINGS_PREFS_PATH,
                ), \
                mock.patch.object(
                    window, "_root_path_exists", side_effect=[False, False]
                ), \
                mock.patch.object(
                    window,
                    "_read_root_file_bytes",
                    side_effect=[source_xml, source_xml, non_exact_xml],
                ), \
                mock.patch.object(window, "_settings_process_ids", return_value=()), \
                mock.patch.object(
                    window, "_write_root_preferences_atomically"
                ) as atomic_write:
            with self.assertRaisesRegex(RuntimeError, "exact validated payload"):
                window._synchronize_ai_preferences_off(serial, "adb")

        atomic_write.assert_called_once_with(
            serial, "adb", expected_xml, cdc_v2.AI_SETTINGS_PREFS_PATH)

    def test_preference_sync_aborts_for_backup_and_unsafe_xml(self):
        window = self._window()
        serial = "unsafe-cache-rk"
        valid = self._preferences_xml().decode("utf-8")
        missing = valid.replace(
            '    <boolean name="ai_dc" value="false" />\n', ""
        ).encode("utf-8")
        duplicate = valid.replace(
            "</map>",
            '    <boolean name="ai_dc" value="true" />\n</map>',
        ).encode("utf-8")
        malformed = valid.replace("</map>", "").encode("utf-8")

        with mock.patch.object(
                    window,
                    "_resolve_ai_preferences_path",
                    return_value=cdc_v2.AI_SETTINGS_PREFS_PATH,
                ), \
                mock.patch.object(
                    window, "_root_path_exists", return_value=True
                ), \
                mock.patch.object(window, "_read_root_file_bytes") as read_file, \
                mock.patch.object(
                    window, "_write_root_preferences_atomically"
                ) as atomic_write:
            with self.assertRaisesRegex(RuntimeError, "backup exists"):
                window._synchronize_ai_preferences_off(serial, "adb")
        read_file.assert_not_called()
        atomic_write.assert_not_called()

        for name, xml_data in (
                ("missing", missing),
                ("duplicate", duplicate),
                ("malformed", malformed)):
            with self.subTest(name=name), \
                    mock.patch.object(
                        window,
                        "_resolve_ai_preferences_path",
                        return_value=cdc_v2.AI_SETTINGS_PREFS_PATH,
                    ), \
                    mock.patch.object(
                        window, "_root_path_exists", return_value=False
                    ), \
                    mock.patch.object(
                        window, "_read_root_file_bytes", return_value=xml_data
                    ), \
                    mock.patch.object(
                        window, "_settings_process_ids"
                    ) as process_ids, \
                    mock.patch.object(
                        window, "_write_root_preferences_atomically"
                    ) as atomic_write:
                with self.assertRaisesRegex(
                        RuntimeError, "Unsafe Settings preference XML"):
                    window._synchronize_ai_preferences_off(serial, "adb")
                process_ids.assert_not_called()
                atomic_write.assert_not_called()

    def test_preference_sync_failure_propagates_to_guard_status(self):
        window = self._window()
        serial = "cache-write-error-rk"
        completion = mock.Mock()
        snapshot = build_off_property_map()

        with mock.patch.object(window, "active_serial", return_value=serial), \
                mock.patch.object(
                    window,
                    "_read_ai_pq_state",
                    return_value=((0, ""), snapshot),
                ), \
                mock.patch.object(
                    window, "worker", side_effect=lambda callback: callback()
                ), \
                mock.patch.object(
                    window,
                    "ui",
                    side_effect=lambda callback, *args: callback(*args),
                ), \
                mock.patch.object(window, "_root_access_mode", return_value="adb"), \
                mock.patch.object(
                    window,
                    "_synchronize_ai_preferences_off",
                    side_effect=RuntimeError("staging push failed"),
                ):
            window.enforce_ai_pq_off(
                silent=True, on_complete=completion, apply_all=False)

        self.assertIn("Protection failed", window.ai_pq_status_label.text())
        self.assertIn("staging push failed", window.ai_pq_status_label.text())
        self.assertEqual(window.ai_pq_status_label.property("tone"), "error")
        self.assertNotIn("7/7 protected", window.ai_pq_status_label.text())
        completion.assert_called_once_with(False)
        self.assertTrue(window._ai_guard_lock.acquire(blocking=False))
        window._ai_guard_lock.release()

    def test_atomic_preference_write_stages_metadata_moves_and_cleans(self):
        window = self._window()
        serial = "atomic-write-rk"
        token = "a1b2c3"
        payload = self._preferences_xml()
        captured = {}

        def adb_command(*args, **_kwargs):
            if args and args[0] == "push":
                captured["local_path"] = args[1]
                captured["payload"] = Path(args[1]).read_bytes()
            return 0, ""

        token_value = mock.Mock()
        token_value.hex = token
        with mock.patch.object(cdc_v2.uuid, "uuid4", return_value=token_value), \
                mock.patch.object(
                    cdc_v2.legacy, "adb_command", side_effect=adb_command
                ) as adb_mock:
            window._write_root_preferences_atomically(serial, "adb", payload)

        remote_stage = f"/data/local/tmp/cdc-ai-prefs-{token}.xml"
        remote_temp = f"{cdc_v2.AI_SETTINGS_PREFS_DIR}/.cdc-ai-prefs-{token}.tmp"
        calls = [item.args for item in adb_mock.call_args_list]
        self.assertEqual(captured["payload"], payload)
        self.assertFalse(Path(captured["local_path"]).exists())
        self.assertIn(("push", captured["local_path"], remote_stage), calls)
        self.assertIn(("shell", "cp", remote_stage, remote_temp), calls)
        self.assertIn(("shell", "chown", "system:system", remote_temp), calls)
        self.assertIn(("shell", "chmod", "0660", remote_temp), calls)
        self.assertIn(("shell", "restorecon", remote_temp), calls)
        self.assertIn(
            ("shell", "mv", "-f", remote_temp, cdc_v2.AI_SETTINGS_PREFS_PATH),
            calls,
        )
        ordered_operations = (
            ("push", captured["local_path"], remote_stage),
            ("shell", "cp", remote_stage, remote_temp),
            ("shell", "chown", "system:system", remote_temp),
            ("shell", "chmod", "0660", remote_temp),
            ("shell", "restorecon", remote_temp),
            ("shell", "mv", "-f", remote_temp, cdc_v2.AI_SETTINGS_PREFS_PATH),
            ("shell", "restorecon", cdc_v2.AI_SETTINGS_PREFS_PATH),
            ("shell", "sync"),
        )
        self.assertEqual(
            [calls.index(operation) for operation in ordered_operations],
            sorted(calls.index(operation) for operation in ordered_operations),
        )
        self.assertIn(("shell", "sync"), calls)
        self.assertIn(("shell", "rm", "-f", remote_stage), calls)

    def test_automatic_guard_never_writes_preserved_face_capability_gate(self):
        window = self._window()
        serial = "preserved-face-gate-rk3576"
        preserved_property = PRESERVED_FIRMWARE_PROPERTIES[0]
        snapshot = build_off_property_map()
        snapshot[preserved_property] = "1"

        with mock.patch.object(window, "active_serial", return_value=serial), \
                mock.patch.object(
                    window,
                    "_read_ai_pq_state",
                    side_effect=[((0, ""), dict(snapshot))] * 4,
                ), \
                mock.patch.object(
                    window, "worker", side_effect=lambda callback: callback()
                ), \
                mock.patch.object(
                    window,
                    "ui",
                    side_effect=lambda callback, *args: callback(*args),
                ), \
                mock.patch.object(window, "_root_access_mode", return_value="adb"), \
                mock.patch.object(
                    window, "_synchronize_ai_preferences_off", return_value=()
                ), \
                mock.patch.object(
                    window,
                    "_read_ai_preference_booleans",
                    return_value=(
                        cdc_v2.AI_SETTINGS_PREFS_PATH,
                        self._preferences_off_mapping(),
                    ),
                ), \
                mock.patch.object(
                    cdc_v2.legacy, "adb_command", return_value=(0, "")
                ) as adb_command, \
                mock.patch.object(cdc_v2.time, "sleep"):
            window.enforce_ai_pq_off(silent=True, apply_all=True)

        written_names = {
            item.args[2] for item in self._setprop_calls(adb_command)
        }
        self.assertNotIn(preserved_property, written_names)


if __name__ == "__main__":
    unittest.main()
