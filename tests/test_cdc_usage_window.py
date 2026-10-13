import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for path in (PROJECT_ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import cdc_connection as conn
import cdc_usage as usage
import cdc_usage_panel
import cdc_v2
from test_cdc_connection import SAMPLE_KEY
from usage_fixtures import FakeDevice


class UsageWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["cdc-usage-tests"])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.app.setStyleSheet(cdc_v2.V2_STYLESHEET)

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.settings_path = str(Path(self.temp_dir.name) / "cdc-usage-test.ini")
        self.data_dir = Path(self.temp_dir.name) / "appdata"
        self.data_dir.mkdir()

        def isolated_settings(*_args, **_kwargs):
            return QSettings(self.settings_path, QSettings.Format.IniFormat)

        self.patches = [
            mock.patch.object(cdc_v2, "QSettings", side_effect=isolated_settings),
            mock.patch.object(cdc_v2.CdcV2Window, "refresh_devices", autospec=True),
            mock.patch.object(conn, "app_data_dir", return_value=self.data_dir),
            # Never adopt the real Downloads key into the test store.
            mock.patch.object(os.path, "expanduser",
                              side_effect=lambda p: p.replace("~", self.temp_dir.name)),
        ]
        for patch in self.patches:
            patch.start()
        self.windows = []
        self.adb_calls = []
        self.replies = []

    def tearDown(self):
        for window in reversed(self.windows):
            window.ai_guard_timer.stop()
            window._usage_timer.stop()
            window.scrcpy_proc = None
            for timer_id in list(window._timers):
                window.after_cancel(timer_id)
            window.close()
            window.deleteLater()
        self._drain()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp_dir.cleanup()

    def _drain(self, cycles=4):
        for _ in range(cycles):
            self.app.processEvents()

    def _window(self):
        window = cdc_v2.CdcV2Window()
        self.windows.append(window)
        window.showMaximized()
        self._drain()
        return window

    def _fake_adb(self, *args, serial=None, timeout=60, telemetry=True):
        self.adb_calls.append((args, serial, timeout, telemetry))
        reply = self.replies.pop(0) if self.replies else "error: device offline"
        return (0, reply, 0.1)

    def _connected(self, window, address="127.0.0.1:50001"):
        """Run the worker inline and pretend ADB has a device at ``address``."""
        window.worker = lambda job: job()
        window.ui = lambda callback, *a, **k: callback(*a, **k)
        window.active_serial = lambda: address
        return mock.patch.object(cdc_v2.legacy, "adb_command", side_effect=self._fake_adb)

    # ---------- menu and visibility ----------
    def test_view_menu_offers_show_usage_with_ctrl_u_and_starts_hidden(self):
        window = self._window()
        view = window._menus["&View"]
        self.assertIn(window.usage_action, view.actions())
        self.assertEqual(window.usage_action.text(), "Show usage")
        self.assertTrue(window.usage_action.isCheckable())
        self.assertEqual(window.usage_action.shortcut(), QKeySequence("Ctrl+U"))
        self.assertFalse(window.usage_action.isChecked())
        self.assertFalse(window.usage_panel.isVisible())
        self.assertFalse(window._usage_timer.isActive())

    def test_showing_usage_puts_the_card_in_the_left_pane_and_polls(self):
        window = self._window()
        window.usage_action.trigger()
        self._drain()
        self.assertTrue(window.usage_panel.isVisible())
        self.assertTrue(window.sidebar.isAncestorOf(window.usage_panel))
        self.assertTrue(window._usage_timer.isActive())
        self.assertEqual(window._usage_timer.interval(), cdc_v2.USAGE_INTERVAL_MS)
        window.usage_action.trigger()
        self._drain()
        self.assertFalse(window.usage_panel.isVisible())
        self.assertFalse(window._usage_timer.isActive())

    def test_shortcut_works_while_the_menu_bar_is_hidden(self):
        window = self._window()
        window.menuBar().hide()
        window.activateWindow()
        QTest.keyClick(window, Qt.Key.Key_U, Qt.KeyboardModifier.ControlModifier)
        self._drain()
        self.assertTrue(window.usage_panel.isVisible())

    def test_usage_card_sits_directly_under_the_device_card(self):
        window = self._window()
        layout = window.usage_panel.parentWidget().layout()
        self.assertEqual(layout.indexOf(window.usage_panel), 1)

    def test_close_button_hides_and_unchecks(self):
        window = self._window()
        window._set_usage_visible(True)
        self._drain()
        window.usage_panel.close_button.click()
        self._drain()
        self.assertFalse(window.usage_panel.isVisible())
        self.assertFalse(window.usage_action.isChecked())

    def test_sidebar_sizing_pass_leaves_the_card_alone(self):
        window = self._window()
        self.assertNotEqual(window.usage_panel.close_button.height(), cdc_v2.CONTROL_HEIGHT)
        self.assertNotEqual(
            window.usage_panel.close_button.objectName(), "StandardAction")

    def test_visibility_and_selected_metric_survive_a_restart(self):
        window = self._window()
        window._set_usage_visible(True)
        window.usage_panel.select("storage")
        window.close()
        self._drain()
        second = self._window()
        self.assertTrue(second.usage_action.isChecked())
        self.assertTrue(second.usage_panel.isVisible())
        self.assertEqual(second.usage_panel.selected_metric(), "storage")

    def test_showing_usage_reveals_a_hidden_sidebar(self):
        window = self._window()
        window._set_sidebar_visible(False)
        window._set_usage_visible(True)
        self._drain()
        self.assertTrue(window.sidebar.isVisible())
        self.assertTrue(window.usage_panel.isVisible())

    def test_card_does_not_widen_the_sidebar_at_its_minimum(self):
        window = self._window()
        window._set_usage_visible(True)
        window.main_splitter.setSizes([cdc_v2.SIDEBAR_MIN_WIDTH, 1200])
        self._drain()
        self.assertLessEqual(
            window.usage_panel.minimumSizeHint().width(), cdc_v2.SIDEBAR_MIN_WIDTH)

    # ---------- polling ----------
    def test_no_device_means_no_adb_call(self):
        window = self._window()
        with self._connected(window, address=None) as adb:
            window._set_usage_visible(True)
            window._usage_tick()
        adb.assert_not_called()
        self.assertEqual(window.usage_panel.status_label.text(), "No device")

    def test_live_readings_reach_the_panel(self):
        window = self._window()
        device = FakeDevice()
        self.replies = [device.reply()]
        device.advance(busy=0.5)
        self.replies.append(device.reply())
        with self._connected(window):
            window._set_usage_visible(True)
            window._usage_tick()
            window._usage_tick()
        args, serial, timeout, telemetry = self.adb_calls[0]
        self.assertEqual(args, ("shell", usage.USAGE_SCRIPT))
        self.assertEqual(serial, "127.0.0.1:50001")
        self.assertFalse(telemetry, "polling must not flood the command log")
        self.assertLessEqual(timeout, 10)
        panel = window.usage_panel
        self.assertTrue(panel.has_samples())
        self.assertEqual(panel.status_label.text(), "Live · 2 s")
        self.assertEqual(panel.status_label.property("tone"), "online")
        self.assertNotEqual(panel.big_value.text(), "—")
        self.assertIn("GB", panel.rows["memory"].value_label.text())
        self.assertIn("full", panel.rows["storage"].value_label.text())

    def test_a_slow_tunnel_skips_ticks_instead_of_queueing(self):
        window = self._window()
        pending = []
        window.worker = pending.append
        window.active_serial = lambda: "127.0.0.1:50001"
        window._set_usage_visible(True)
        window._usage_tick()
        window._usage_tick()
        window._usage_tick()
        self.assertEqual(len(pending), 1)

    def test_hidden_or_minimised_window_does_not_poll(self):
        window = self._window()
        pending = []
        window.worker = pending.append
        window.active_serial = lambda: "127.0.0.1:50001"
        window._usage_tick()                 # card hidden
        window._set_usage_visible(True)
        pending.clear()
        window.showMinimized()
        self._drain()
        window._usage_tick()
        self.assertEqual(pending, [])

    def test_failures_are_reported_once_and_recover(self):
        window = self._window()
        lines = []
        window.logline = lines.append
        with self._connected(window):
            window._set_usage_visible(True)
            for _ in range(5):
                window._usage_tick()
            self.assertEqual(window.usage_panel.status_label.text(), "Not answering")
            self.assertEqual(window.usage_panel.status_label.property("tone"), "error")
            self.assertEqual(sum("[Usage]" in line for line in lines), 1)
            self.replies = [FakeDevice().reply()]
            window._usage_tick()
        self.assertEqual(window.usage_panel.status_label.property("tone"), "online")

    def test_a_different_address_starts_the_history_over(self):
        window = self._window()
        device = FakeDevice()
        self.replies = [device.reply()]
        with self._connected(window):
            window._set_usage_visible(True)
            window._usage_tick()
            self.assertTrue(window.usage_panel.has_samples())
            window.active_serial = lambda: "127.0.0.1:50002"
            window.worker = lambda job: None      # leave the reply pending
            window._usage_tick()
        self.assertFalse(window.usage_panel.has_samples())

    def test_a_moved_lease_on_the_same_address_starts_over(self):
        window = self._window()
        lines = []
        window.logline = lines.append
        first = FakeDevice(serial="SN2026020201944")
        second = FakeDevice(serial="SN2026020201962", uptime=99_000.0)
        self.replies = [first.reply()]
        first.advance()
        self.replies.append(first.reply())
        self.replies.append(second.reply())
        with self._connected(window):
            window._set_usage_visible(True)
            for _ in range(3):
                window._usage_tick()
        history = window.usage_panel._history["cpu"]
        self.assertEqual(list(history), [None], "old device's graph must be dropped")
        self.assertTrue(any("SN2026020201962" in line for line in lines))

    def test_reply_for_an_old_address_is_ignored(self):
        window = self._window()
        window._usage_serial = "127.0.0.1:50002"
        window._usage_in_flight = True
        window._usage_on_reply("127.0.0.1:50001", 0, FakeDevice().reply())
        self.assertFalse(window._usage_in_flight)
        self.assertFalse(window.usage_panel.has_samples())

    # ---------- setup key ----------
    def test_key_chosen_during_setup_is_imported_on_first_launch(self):
        staged = conn.staged_setup_key_path()
        staged.write_text(SAMPLE_KEY.replace("\n", "\r\n"), encoding="ascii")
        window = self._window()
        self.assertTrue(conn.has_stored_key())
        self.assertFalse(staged.exists())
        self.assertNotIn("\r", conn.stored_key_path().read_text(encoding="ascii"))
        self.assertFalse(window.key_button.isVisible())

    def test_bad_setup_key_is_reported_and_removed(self):
        staged = conn.staged_setup_key_path()
        staged.write_text("ssh-rsa AAAAB3Nza public half", encoding="ascii")
        lines = []
        with mock.patch.object(cdc_v2.CdcV2Window, "logline",
                               lambda _self, text: lines.append(text)):
            self._window()
        self.assertFalse(staged.exists())
        self.assertFalse(conn.has_stored_key())
        self.assertTrue(any("chosen during setup" in line for line in lines))


class UsagePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["cdc-usage-tests"])
        cls.app.setStyleSheet(cdc_v2.V2_STYLESHEET)

    def setUp(self):
        self.panel = cdc_usage_panel.UsagePanel(2)
        self.panel.resize(260, 700)
        self.panel.show()
        self.addCleanup(self.panel.deleteLater)

    def _feed(self, device, ticks=3):
        sampler = usage.UsageSampler()
        for _ in range(ticks):
            device.advance()
            self.panel.add_sample(sampler.feed(device.reply()))

    def test_every_metric_renders_without_and_with_data(self):
        for key in cdc_usage_panel.METRIC_KEYS:
            self.panel.select(key)
            self.panel.grab()
        self._feed(FakeDevice())
        for key in cdc_usage_panel.METRIC_KEYS:
            self.panel.select(key)
            self.panel.grab()
            self.assertEqual(self.panel.detail_title.text(),
                             dict((k, n) for k, n, _c in cdc_usage_panel.METRICS)[key])

    def test_memory_and_storage_show_occupied_and_available(self):
        self._feed(FakeDevice())
        self.panel.select("memory")
        self.assertTrue(self.panel.capacity_bar.isVisible())
        self.assertIn("In use", self.panel.legend_primary.text.text())
        self.assertIn("Available", self.panel.legend_secondary.text.text())
        self.panel.select("storage")
        self.assertTrue(self.panel.capacity_bar.isVisible())
        self.assertIn("Used", self.panel.legend_primary.text.text())
        self.assertIn("Free", self.panel.legend_secondary.text.text())
        self.assertIn("free of", self.panel.detail_subtitle.text())

    def test_cpu_shows_one_bar_per_core(self):
        self._feed(FakeDevice(cores=4))
        self.panel.select("cpu")
        self.assertTrue(self.panel.core_strip.isVisible())
        self.assertEqual(len(self.panel.core_strip._values), 4)

    def test_missing_gpu_says_so_instead_of_zero(self):
        self._feed(FakeDevice(gpu=False))
        self.assertEqual(self.panel.rows["gpu"].value_label.text(), "Not reported")
        self.panel.select("gpu")
        self.assertEqual(self.panel.big_value.text(), "—")
        self.assertIn("Not reported", self.panel.detail_subtitle.text())

    def test_history_is_capped_at_two_minutes(self):
        self._feed(FakeDevice(), ticks=usage.HISTORY_LENGTH + 25)
        self.assertEqual(len(self.panel._history["cpu"]), usage.HISTORY_LENGTH)

    def test_rows_select_by_click_and_arrow_keys(self):
        changes = []
        self.panel.metric_changed.connect(changes.append)
        QTest.mouseClick(self.panel.rows["network"], Qt.MouseButton.LeftButton)
        self.assertEqual(self.panel.selected_metric(), "network")
        self.assertTrue(self.panel.rows["network"].property("selected"))
        QTest.keyClick(self.panel.rows["network"], Qt.Key.Key_Down)
        self.assertEqual(self.panel.selected_metric(), "cpu")      # wraps
        QTest.keyClick(self.panel.rows["cpu"], Qt.Key.Key_Up)
        self.assertEqual(self.panel.selected_metric(), "network")
        self.assertEqual(changes, ["network", "cpu", "network"])

    def test_clear_returns_to_the_empty_state(self):
        self._feed(FakeDevice())
        self.panel.clear()
        self.assertFalse(self.panel.has_samples())
        for row in self.panel.rows.values():
            self.assertEqual(row.value_label.text(), "—")

    def test_network_scale_follows_the_traffic(self):
        device = FakeDevice()
        sampler = usage.UsageSampler()
        self.panel.add_sample(sampler.feed(device.reply()))
        device.advance(rx_bps=4.1e6)
        self.panel.add_sample(sampler.feed(device.reply()))
        self.panel.select("network")
        self.assertEqual(self.panel.caption_right.text(), "5.0 Mbps")


if __name__ == "__main__":
    unittest.main()
