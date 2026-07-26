#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apple Silicon macOS entry point for Convrse Device Control.

Windows CDC embeds scrcpy by re-parenting its Win32 window. macOS does not
provide an equivalent supported cross-process Cocoa embedding API, so this
edition keeps the CDC controls in the main app and opens scrcpy as a native,
independently resizable Mac window. All device operations, stream profiles,
audio routing and the rooted display guard remain shared with V2.3.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

import cdc_v2 as base


MAC_APP_VERSION = "2.4.0"
MAC_BUNDLE_ID = "ai.convrse.device-control"


def configure_source_runtime():
    """Use the staged portable runtime when running source directly on a Mac."""
    if sys.platform != "darwin" or getattr(sys, "frozen", False):
        return
    runtime_root = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "scrcpy-runtime-macos-arm64")
    adb = os.path.join(runtime_root, "adb")
    scrcpy = os.path.join(runtime_root, "scrcpy")
    if os.path.isfile(adb):
        base.legacy.ADB = adb
    if os.path.isfile(scrcpy):
        base.legacy.SCRCPY = scrcpy


configure_source_runtime()


def build_native_scrcpy_command(serial, title, config):
    """Return the reviewed stream command without Windows embedding flags."""
    windows_command = base.CdcV2Window._build_scrcpy_command(serial, title, config)
    command = []
    skip_value = False
    for part in windows_command:
        if skip_value:
            skip_value = False
            continue
        if part in ("--window-x", "--window-y"):
            skip_value = True
            continue
        if part == "--window-borderless":
            continue
        command.append(part)
    return command


class MacCdcWindow(base.CdcV2Window):
    """CDC shell adapted to native macOS window and menu behavior."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(base.APP_NAME)

    def _build_ui(self):
        super()._build_ui()

        # macOS owns a persistent system menu bar. The Windows Alt-to-reveal
        # interaction must not intercept Option or hide menus after actions.
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.menuBar().setNativeMenuBar(True)
        self.menuBar().show()

        # A CDC full-screen shell would only enlarge the placeholder because
        # scrcpy is a separate Cocoa window. Use scrcpy's green window control
        # for full-screen instead and do not expose a misleading CDC action.
        self.header_focus_button.hide()
        self.focus_action.setVisible(False)
        self.focus_action.setEnabled(False)
        try:
            self.mirror_frame.doubleClicked.disconnect()
        except (RuntimeError, TypeError):
            pass

        self._configure_native_menu_roles()
        self._set_native_mirror_panel(False)

    def _configure_native_menu_roles(self):
        for menu_action in self.menuBar().actions():
            menu = menu_action.menu()
            if menu is None:
                continue
            for action in menu.actions():
                plain = action.text().replace("&", "")
                if plain == "Exit":
                    action.setText(f"Quit {base.APP_NAME}")
                    action.setShortcut(QKeySequence.StandardKey.Quit)
                    action.setMenuRole(QAction.MenuRole.QuitRole)
                elif plain == "About Convrse Device Control":
                    action.setMenuRole(QAction.MenuRole.AboutRole)
                elif plain == "Refresh device":
                    action.setShortcut(QKeySequence("Ctrl+R"))

    def _hide_menu_bar(self):
        """Native Mac menus are intentionally always available."""
        return

    def _hide_menu_after_action(self):
        return

    def _toggle_menu_bar(self):
        return

    def _set_native_mirror_panel(self, running):
        empty_state = getattr(self.mirror_frame, "empty_state", None)
        if empty_state is None:
            return
        labels = {label.objectName(): label for label in empty_state.findChildren(QLabel)}
        if running:
            copy = {
                "Overline": "NATIVE MIRROR ACTIVE",
                "EmptyTitle": "Your device is open in a separate window",
                "Secondary": (
                    "Use the scrcpy window for control, audio and drag-and-drop. "
                    "Close it or press Stop mirror here when finished."
                ),
            }
        else:
            copy = {
                "Overline": "MAC MIRROR READY",
                "EmptyTitle": "The device opens in its own native window",
                "Secondary": (
                    "Connect a device, choose a preset, then start the mirror. "
                    "No Terminal commands are required."
                ),
            }
        for object_name, text in copy.items():
            label = labels.get(object_name)
            if label is not None:
                label.setText(text)
                label.setWordWrap(True)

    @staticmethod
    def _build_scrcpy_command(serial, title, config):
        return build_native_scrcpy_command(serial, title, config)

    def _watch_scrcpy_window(self, proc, _title, generation):
        """Confirm the native scrcpy process stayed alive, then mark it live."""
        for _ in range(15):
            if not self._is_current_mirror(proc, generation):
                return
            if proc.poll() is not None:
                return
            if self._mirror_state in ("stopping", "restarting"):
                return
            time.sleep(0.1)
        self.ui(self._mark_native_mirror_running, proc, generation)

    def _mark_native_mirror_running(self, proc, generation):
        if (not self._is_current_mirror(proc, generation)
                or proc.poll() is not None
                or self._mirror_state in ("stopping", "restarting")):
            return
        self._set_native_mirror_panel(True)
        self._set_mirror_state(
            "running", f"{self.selected_preset} stream is live in the mirror window")
        self.logline("[Mirror] Native macOS scrcpy window opened.")
        self.worker(lambda: self._refresh_mirror_source_size(proc, generation))

    def _clear_mirror_embed(self):
        # There is no foreign QWidget on macOS, but keep the same lifecycle and
        # metrics reset contract used by the cross-platform poller.
        self.scrcpy_hwnd = None
        self.mirror_aspect = None
        self.mirror_frame.clear_window()
        self._scrcpy_fps = None
        self._scrcpy_resolution = None
        self._update_stream_metrics_label()
        self._set_native_mirror_panel(False)

    def toggle_focus_mode(self):
        self.status_var.set(
            "Use the mirror window's green control for full screen; Escape exits it")

    def connect_tunnel(self):
        # OpenSSH on macOS refuses private keys that are readable by other users.
        # Fix the selected key in-app so connecting never requires chmod in Terminal.
        raw_path = self.pem_var.get().strip().strip('"')
        pem_path = os.path.abspath(os.path.expanduser(raw_path)) if raw_path else ""
        if pem_path and os.path.isfile(pem_path):
            try:
                os.chmod(pem_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError as exc:
                self.logline(f"[SSH] Could not restrict private-key permissions: {exc}")
        return super().connect_tunnel()

    def _open_logs_folder(self):
        folder = os.path.join(os.path.expanduser("~"), "Library", "Logs", "Convrse CDC")
        os.makedirs(folder, exist_ok=True)
        try:
            subprocess.Popen(["open", folder])
        except Exception as exc:
            base.v1.QtMessageBoxAdapter.showerror(
                base.APP_NAME, f"Could not open logs folder:\n{exc}")

    def _show_shortcuts(self):
        QMessageBox.information(
            self,
            "Keyboard shortcuts",
            "⌘B  Show/hide sidebar\n"
            "⌘J  Show/hide command log\n"
            "⌘L  Focus command log\n"
            "⌘R  Refresh devices\n"
            "⌘0  Reset layout\n\n"
            "Use the mirror window's green control for full screen and Escape to exit.",
        )


def main():
    if sys.platform != "darwin":
        raise SystemExit(
            "The macOS edition must be launched on Apple Silicon macOS. "
            "Use cdc_v2.py or the V2.3 EXE on Windows."
        )

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(base.APP_NAME)
    app.setApplicationDisplayName(base.APP_NAME)
    app.setApplicationVersion(MAC_APP_VERSION)
    app.setOrganizationName("Convrse")
    app.setOrganizationDomain("convrse.ai")
    app.setWindowIcon(QIcon(base.v1.resource_path("assets/convrse-logo.png")))
    app.setStyle("Fusion")
    app.setStyleSheet(
        base.V2_STYLESHEET
        .replace('"Segoe UI"', '"SF Pro Text"')
        .replace('"Cascadia Mono"', '"SF Mono"')
    )

    window = MacCdcWindow()
    window.showMaximized()
    window.raise_()
    window.activateWindow()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
