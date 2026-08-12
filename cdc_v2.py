#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convrse Device Control V2 — mirror-first Qt operations workspace."""

from collections import OrderedDict
import ctypes
from dataclasses import dataclass
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from xml.etree import ElementTree

from PySide6.QtCore import QEvent, QSize, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap, QShortcut, QWindow
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import cdc_connection as conn
import cdc_health as health
import cdc_qt as v1
import scrcpy_remote as legacy
from cdc_ai_guard import (
    AI_DISPLAY_TOGGLES,
    AUTO_ENFORCE_ON_CONNECT,
    CapabilityLevel,
    ToggleState,
    evaluate_getprop_mapping,
    parse_shared_preferences_booleans,
    plan_guard_on_connection,
    sync_ai_display_preferences_off,
)


APP_NAME = legacy.APP_NAME
APP_VERSION = "2.4.0"


def _offscreen_origin():
    """A point past the bottom-right of every monitor.

    A hardcoded 4000,4000 sits inside the desktop on a multi-monitor or 4K
    setup, which is where an operator would see the scrcpy window appear on its
    own before it is embedded.
    """
    try:
        from PySide6.QtGui import QGuiApplication
        geometry = QGuiApplication.primaryScreen().virtualGeometry()
        return geometry.right() + 200, geometry.bottom() + 200
    except Exception:
        return 4000, 4000


def _qt_version():
    try:
        from PySide6 import __version__ as pyside_version
        return pyside_version
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class StreamPreset:
    """One reviewed video target. A max size of zero means device-native."""

    resolution: str
    max_size: int
    bitrate_bps: int
    max_fps: int | None


@dataclass(frozen=True)
class DeviceStreamCapabilities:
    """Per-device facts used to resolve Native, Max and encoder selection."""

    width: int | None = None
    height: int | None = None
    refresh_hz: float | None = None
    sdk: int | None = None
    h264_encoder: str | None = None
    probe_error: str | None = None


@dataclass(frozen=True)
class ResolvedStreamConfig:
    name: str
    resolution: str
    max_size: int
    bitrate_bps: int
    max_fps: int
    h264_encoder: str | None = None


STREAM_PRESETS = OrderedDict([
    ("Low", StreamPreset("640 \u00d7 360", 640, 750_000, 20)),
    ("Balanced", StreamPreset("960 \u00d7 540", 960, 1_500_000, 25)),
    ("Normal", StreamPreset("1280 \u00d7 720", 1280, 5_000_000, 30)),
    ("Native", StreamPreset("Device native", 0, 8_000_000, 30)),
    ("Max", StreamPreset("Device native", 0, 12_000_000, None)),
])
CUSTOM_PRESET = "Custom"
PROFILE_NAMES = (*STREAM_PRESETS.keys(), CUSTOM_PRESET)
DEFAULT_PRESET = "Balanced"

CUSTOM_RESOLUTIONS = OrderedDict([
    ("640 \u00d7 360", 640),
    ("960 \u00d7 540", 960),
    ("1280 \u00d7 720", 1280),
    ("1920 \u00d7 1080", 1920),
    ("Device native", 0),
])
CUSTOM_FPS_OPTIONS = (20, 25, 30, 60)
DEFAULT_CUSTOM_MAX_SIZE = 960
DEFAULT_CUSTOM_FPS = 60
DEFAULT_CUSTOM_BITRATE_MBPS = 4.0

AUDIO_SOURCE = "output"
AUDIO_CODEC = "opus"
AUDIO_BITRATE = "128K"
AUDIO_BUFFER_MS = 200

PRIMARY_ACTION_HEIGHT = 40
CONTROL_HEIGHT = 36
APP_BAR_CONTROL_HEIGHT = 38
# Widest sidebar content is 239px once the rarely-used controls sit behind their
# disclosures, plus the shell margins and the vertical scroll bar. The old 302px
# floor clipped the right column of the three-wide grids; this one has room.
SIDEBAR_MIN_WIDTH = 272

AI_SETTINGS_PACKAGE = "com.android.tv.settings"
AI_SETTINGS_COMPONENT = f"{AI_SETTINGS_PACKAGE}/.MainSettings"
AI_SETTINGS_PREFS_DIR = (
    f"/data/user_de/0/{AI_SETTINGS_PACKAGE}/shared_prefs"
)
AI_SETTINGS_PREFS_PATH = (
    f"{AI_SETTINGS_PREFS_DIR}/{AI_SETTINGS_PACKAGE}_preferences.xml"
)
AI_SETTINGS_PREFS_BACKUP_PATH = f"{AI_SETTINGS_PREFS_PATH}.bak"


V2_STYLESHEET = v1.APP_STYLESHEET + f"""
QMenuBar {{
    background: {v1.BG_ALT}; color: {v1.MUTED}; border-bottom: 1px solid {v1.BORDER};
    padding: 2px 7px;
}}
QMenuBar::item {{ padding: 5px 9px; border-radius: 4px; }}
QMenuBar::item:selected {{ background: {v1.SURFACE_2}; color: {v1.TEXT}; }}
QMenu {{ background: {v1.SURFACE}; border: 1px solid {v1.BORDER}; padding: 5px; }}
QMenu::item {{ padding: 7px 28px 7px 10px; border-radius: 4px; }}
QMenu::item:selected {{ background: {v1.SURFACE_3}; }}
QWidget#AppBar {{ background: {v1.BG_ALT}; border-bottom: 1px solid {v1.BORDER}; }}
QLabel#CompactTitle {{ font-size: 16px; font-weight: 700; color: {v1.TEXT}; }}
QLabel#DevicePill {{
    color: {v1.MUTED}; background: {v1.SURFACE}; border: 1px solid {v1.BORDER};
    border-radius: 6px; padding: 7px 10px;
}}
QFrame#SidebarV2 {{ background: {v1.SIDEBAR}; border-right: 1px solid {v1.BORDER}; }}
QPushButton#ConnectionAction {{
    min-height: {PRIMARY_ACTION_HEIGHT - 2}px; max-height: {PRIMARY_ACTION_HEIGHT - 2}px;
    padding: 0 12px; font-size: 13px; font-weight: 800;
}}
QPushButton#StandardAction, QPushButton[controlSize="standard"] {{
    min-height: {CONTROL_HEIGHT - 2}px; max-height: {CONTROL_HEIGHT - 2}px;
    padding: 0 10px;
}}
QPushButton#StandardAction[role="nav"] {{
    min-height: {CONTROL_HEIGHT}px; max-height: {CONTROL_HEIGHT}px;
}}
QPushButton#AppBarAction {{
    min-height: {APP_BAR_CONTROL_HEIGHT - 2}px;
    max-height: {APP_BAR_CONTROL_HEIGHT - 2}px;
    padding: 0 12px;
}}
QFrame#CustomProfile {{
    background: {v1.BG_ALT}; border: 1px solid {v1.BORDER}; border-radius: 7px;
}}
QDoubleSpinBox {{
    background: {v1.BG_ALT}; border: 1px solid {v1.BORDER}; border-radius: 5px;
    color: {v1.TEXT}; padding: 7px 9px; min-height: 19px;
}}
QDoubleSpinBox:focus {{ border: 1px solid {v1.BLUE}; }}
QPushButton[role="warning"] {{
    background: #4B3B18; color: #FFE1A0; border-color: #7B6125; font-weight: 700;
}}
QLabel#StreamState {{ color: {v1.MUTED}; padding: 2px; }}
QLabel#StreamState[tone="online"] {{ color: {v1.GREEN}; }}
QLabel#StreamState[tone="pending"] {{ color: {v1.AMBER}; }}
QLabel#StreamState[tone="error"] {{ color: {v1.RED}; }}
QLabel#KeyState {{ font-size: 11px; font-weight: 600; color: {v1.MUTED}; }}
QLabel#KeyState[tone="online"] {{ color: {v1.GREEN}; }}
QLabel#KeyState[tone="error"] {{ color: {v1.AMBER}; }}
QLabel#RouteStrip {{
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 11px; font-weight: 600; color: {v1.SUBTLE};
    background: {v1.BG_ALT}; border: 1px solid {v1.BORDER};
    border-radius: 6px; padding: 7px 9px;
}}
QLabel#RouteStrip[tone="online"] {{ color: {v1.GREEN}; border-color: #1F5643; }}
QLabel#RouteStrip[tone="pending"] {{ color: {v1.AMBER}; border-color: #5A4A1E; }}
QLabel#RouteStrip[tone="error"] {{ color: {v1.RED}; border-color: #5E2833; }}
QFrame#ToggleRow {{ background: {v1.BG_ALT}; border: 1px solid #1C2C46; border-radius: 5px; }}
QLabel#ToggleName {{ font-size: 10px; font-weight: 600; color: {v1.MUTED}; }}
QLabel#ToggleValue {{ font-size: 10px; font-weight: 800; color: {v1.SUBTLE}; }}
QLabel#ToggleValue[tone="online"] {{ color: {v1.GREEN}; }}
QLabel#ToggleValue[tone="error"] {{ color: {v1.RED}; }}
QPushButton#CompactIcon {{
    min-width: {APP_BAR_CONTROL_HEIGHT - 2}px; max-width: {APP_BAR_CONTROL_HEIGHT - 2}px;
    min-height: {APP_BAR_CONTROL_HEIGHT - 2}px;
    max-height: {APP_BAR_CONTROL_HEIGHT - 2}px;
    padding: 0;
}}
QPushButton#LogReveal {{
    min-height: 18px; max-height: 18px; padding: 0 7px; border: 0;
    background: transparent; color: {v1.SUBTLE}; text-align: right;
}}
QPushButton#LogReveal:hover {{ color: {v1.TEXT}; background: {v1.SURFACE_2}; }}
"""


def _role(widget, role):
    widget.setProperty("role", role)
    return widget


def _repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class PresetAdapter:
    """StringVar-compatible surface for legacy status helpers."""

    def __init__(self, owner):
        self.owner = owner

    def get(self):
        return self.owner.selected_preset

    def set(self, value):
        if value in PROFILE_NAMES:
            self.owner.select_preset(value, restart=False)


class TapSelectComboBox(QComboBox):
    """Prevent sidebar scrolling from accidentally changing a closed combo."""

    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


class EqualActionRow(QWidget):
    """Lay out two primary actions at exactly the same pixel size.

    Qt normally gives one button the remainder pixel when a two-column layout
    has an odd width. That produced the visible 1 px discrepancy on macOS.
    Manual aspect-neutral geometry leaves the remainder at the trailing edge
    instead, keeping both controls identical on every platform and scale.
    """

    def __init__(self, spacing=6, parent=None):
        super().__init__(parent)
        self._spacing = int(spacing)
        self._actions = ()
        self.setFixedHeight(PRIMARY_ACTION_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_actions(self, left, right):
        self._actions = (left, right)
        for action in self._actions:
            action.setParent(self)
            action.show()
        self._layout_actions()

    def sizeHint(self):
        return QSize(300, PRIMARY_ACTION_HEIGHT)

    def minimumSizeHint(self):
        return QSize(206, PRIMARY_ACTION_HEIGHT)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_actions()

    def _layout_actions(self):
        if len(self._actions) != 2:
            return
        action_width = max(1, (self.width() - self._spacing) // 2)
        action_height = self.height()
        self._actions[0].setGeometry(0, 0, action_width, action_height)
        self._actions[1].setGeometry(
            action_width + self._spacing, 0, action_width, action_height)


class CaptureButtonAdapter(v1.ButtonAdapter):
    """Keep diagnostic state actionable without exposing an elapsed timer."""

    def configure(self, **kwargs):
        text = str(kwargs.get("text", ""))
        if text.startswith("Stop session"):
            kwargs["text"] = "Stop diagnostic session"
        super().configure(**kwargs)


class V2CommandConsole(v1.CommandConsole):
    toggleRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root_layout = self.layout()
        header = root_layout.itemAt(0).layout()
        for index in reversed(range(header.count())):
            item = header.itemAt(index)
            widget = item.widget()
            if isinstance(widget, QPushButton):
                header.takeAt(index)
                widget.hide()
                widget.deleteLater()
        labels = self.findChildren(QLabel)
        self.title_label = labels[0] if labels else None
        if self.title_label is not None:
            self.title_label.setText("COMMAND LOG")
        self.toggle_button = QPushButton("⌄ Hide")
        self.toggle_button.setObjectName("LogReveal")
        self.toggle_button.setFixedHeight(18)
        self.toggle_button.setToolTip("Show or hide the command log (Ctrl+J)")
        self.toggle_button.clicked.connect(
            lambda _checked=False: self.toggleRequested.emit())
        header.addWidget(self.toggle_button)
        self.set_collapsed_visual(True)

    def set_collapsed_visual(self, collapsed):
        collapsed = bool(collapsed)
        root_layout = self.layout()
        self.output.setVisible(not collapsed)
        if self.title_label is not None:
            self.title_label.setVisible(not collapsed)
        if collapsed:
            root_layout.setContentsMargins(5, 2, 5, 2)
            root_layout.setSpacing(0)
            self.setMinimumHeight(22)
            self.setMaximumHeight(22)
            self.toggle_button.setText("⌃ Command log")
        else:
            root_layout.setContentsMargins(9, 7, 9, 9)
            root_layout.setSpacing(6)
            self.setMaximumHeight(16777215)
            self.setMinimumHeight(120)
            self.toggle_button.setText("⌄ Hide")

class AspectMirrorHost(QFrame):
    """Foreign-window host that always fits and centers without cropping."""

    doubleClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MirrorHost")
        self.setStyleSheet(
            f"QFrame#MirrorHost {{ background: #000; border: 1px solid {v1.BORDER}; "
            "border-radius: 8px; }}")
        self.setMinimumSize(420, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.aspect_ratio = 16 / 9
        self.foreign_window = None
        self.foreign_container = None

        self.empty_state = QWidget(self)
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.addStretch(1)
        overline = v1._label("NO ACTIVE MIRROR", "Overline")
        title = v1._label("Your device will appear here", "EmptyTitle")
        detail = v1._label(
            "Connect a device, choose a preset, then start the mirror.",
            "Secondary",
        )
        for widget in (overline, title, detail):
            widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_layout.addWidget(widget)
        empty_layout.addStretch(1)

    def set_aspect(self, ratio):
        if ratio and ratio > 0:
            self.aspect_ratio = float(ratio)
            self._layout_content()

    def adopt_window(self, hwnd):
        self.clear_window()
        self.foreign_window = QWindow.fromWinId(int(hwnd))
        if self.foreign_window is None:
            raise RuntimeError("Qt could not wrap the scrcpy window")
        self.foreign_container = QWidget.createWindowContainer(
            self.foreign_window, self, Qt.WindowType.FramelessWindowHint)
        self.foreign_container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.empty_state.hide()
        self.foreign_container.show()
        self._layout_content()

    def clear_window(self):
        if self.foreign_container is not None:
            self.foreign_container.hide()
            self.foreign_container.deleteLater()
        self.foreign_container = None
        self.foreign_window = None
        self.empty_state.show()
        self._layout_content()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_content()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _layout_content(self):
        rect = self.contentsRect().adjusted(2, 2, -2, -2)
        self.empty_state.setGeometry(rect)
        if self.foreign_container is None or rect.width() <= 0 or rect.height() <= 0:
            return
        ratio = self.aspect_ratio or (16 / 9)
        width = rect.width()
        height = int(width / ratio)
        if height > rect.height():
            height = rect.height()
            width = int(height * ratio)
        x = rect.x() + (rect.width() - width) // 2
        y = rect.y() + (rect.height() - height) // 2
        self.foreign_container.setGeometry(x, y, max(1, width), max(1, height))


class KeyImportDialog(QDialog):
    """Take the SSH key once, by paste or by file.

    Pasting is the default because the key usually arrives in a chat message,
    and asking someone to save it somewhere first is the step that got skipped.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add SSH key")
        self.setMinimumWidth(520)
        self.mode = "paste"
        self.key_text = ""
        self.file_path = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)

        intro = QLabel(
            "This is stored once on this computer, with access limited to your "
            "Windows account. You will not be asked for it again.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.paste_radio = QRadioButton("Paste the key text")
        self.paste_radio.setChecked(True)
        self.paste_radio.toggled.connect(self._sync_mode)
        layout.addWidget(self.paste_radio)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "-----BEGIN RSA PRIVATE KEY-----\n…\n-----END RSA PRIVATE KEY-----")
        self.editor.setMinimumHeight(150)
        layout.addWidget(self.editor)

        self.file_radio = QRadioButton("Use a .pem file instead")
        self.file_radio.toggled.connect(self._sync_mode)
        layout.addWidget(self.file_radio)

        file_row = QHBoxLayout()
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("Path to the .pem file")
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)
        # Browse and the path box stay live whichever mode is selected. Greying
        # them out until the radio above was ticked made Browse look broken --
        # the operator's first instinct is to press Browse, not to pick a mode
        # first, so pressing it simply switches to file mode.
        file_row.addWidget(self.file_edit, 1)
        file_row.addWidget(self.browse_button)
        layout.addLayout(file_row)
        self.file_edit.textChanged.connect(self._path_typed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _sync_mode(self):
        self.mode = "paste" if self.paste_radio.isChecked() else "file"

    def _path_typed(self, text):
        """Typing or dropping a path is itself a choice of file mode."""
        if text.strip() and not self.file_radio.isChecked():
            self.file_radio.setChecked(True)

    def _browse(self):
        start = self.file_edit.text().strip() or os.path.join(
            os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(start):
            start = os.path.dirname(start) or os.path.expanduser("~")
        path, _selected = QFileDialog.getOpenFileName(
            self, "Select SSH private key", start,
            "PEM private key (*.pem);;All files (*.*)")
        if path:
            self.file_edit.setText(path)
            self.file_radio.setChecked(True)

    def _accept(self):
        if self.mode == "file":
            self.file_path = self.file_edit.text().strip()
            if not self.file_path:
                QMessageBox.warning(self, "Add SSH key", "Choose a .pem file first.")
                return
        else:
            self.key_text = self.editor.toPlainText()
            if not self.key_text.strip():
                QMessageBox.warning(self, "Add SSH key", "Paste the key text first.")
                return
        self.accept()


class CdcV2Window(v1.CdcMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        # Remain usable on 1366×768 screens at Windows 150% scaling.
        self.setMinimumSize(820, 500)
        # Every tick of this one runs getprop/setprop across the SSH tunnel, so
        # it is paced for a remote link rather than a USB cable. The properties
        # it watches drift on reboot or a vendor service restart, not
        # second-to-second.
        self.ai_guard_timer.setInterval(15000)
        # It also runs only while display protection is on. Leaving it ticking
        # to discover it has nothing to do just burns a wake-up every interval.
        self._sync_guard_timer()
        QTimer.singleShot(0, self._restore_layout)

    # ---------- V2 shell ----------
    def _build_ui(self):
        # Keep the established settings namespace so existing connection and
        # profile choices migrate into V2.3.4 without a brittle one-off copier.
        self.settings = QSettings("Convrse", "Convrse Device Control V2.1")
        saved_preset = self.settings.value("stream/preset", DEFAULT_PRESET, str)
        self.selected_preset = saved_preset if saved_preset in PROFILE_NAMES else DEFAULT_PRESET
        self._display_protection_enabled = self._bool_value(
            self.settings.value("device/display_protection_enabled", False),
            False,
        )
        self._mirror_state = "stopped"
        self._mirror_requested_stop = False
        self._pending_restart = False
        self._scrcpy_fps = None
        self._scrcpy_resolution = None
        self._metrics_tick = 0
        self._focus_mode = False
        self._console_collapsed = True
        self._saved_console_sizes = [690, 170]
        self._focus_restore = {}
        self._alt_key_down = False
        self._mirror_generation = 0
        self._closing = False
        self._last_main_splitter_state = None
        self._last_workspace_splitter_state = None
        self._root_mode_by_serial = {}
        self._root_failure_reason_by_serial = {}
        self._guard_status_by_serial = {}
        self._guard_failure_reason_by_serial = {}
        self._guard_generation = 0
        self._ai_preferences_path_by_serial = {}
        self._stream_capabilities = {}
        self._stream_probe_generation = 0
        self._stream_probe_serial = None
        self._stream_encoder_bypass = set()
        self._scrcpy_output_lines = []
        self._current_stream_config = None
        self._external_tunnel = False
        # The local socket and the gateway's leased port are separate values.
        self.ssh_remote_port = None
        self._connected_identity = None
        self._pending_local_port = None
        self._pending_remote_port = None
        self._device_watch_timer = None
        self._device_watch_target = ""
        self._device_watch_ticks = 0
        # ADB address -> DeviceIdentity, so the list can show names.
        self._identity_by_address = {}
        self._last_health_result = None
        self._health_recheck_pending = False

        self._build_menus()

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.app_bar = self._build_app_bar()
        outer.addWidget(self.app_bar)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(6)
        self.sidebar = self._build_sidebar()
        self.main_splitter.addWidget(self.sidebar)
        self.main_splitter.addWidget(self._build_workspace())
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([SIDEBAR_MIN_WIDTH, 1200])
        outer.addWidget(self.main_splitter, 1)

        # Visible unless the operator hid it with Alt last session.
        self.menuBar().setVisible(
            self._bool_value(self.settings.value("ui/menu_bar_visible", True), True))
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _build_menus(self):
        menu_bar = self.menuBar()
        menu_bar.clear()
        # Menus are owned by the menu bar, but keeping our own references makes
        # the ownership explicit and keeps the objects reachable for tests.
        self._menus = {}

        file_menu = self._add_menu("&File")
        self._menu_action(file_menu, "Add or change SSH key…", self.select_pem_key, "Ctrl+O")
        self._menu_action(file_menu, "Install APK…", self.install_apk, "Ctrl+I")
        file_menu.addSeparator()
        self.display_protection_action = self._menu_action(
            file_menu,
            "Display protection",
            self._set_display_protection_enabled,
            checkable=True,
        )
        self.display_protection_action.setChecked(
            self._display_protection_enabled)
        self._sync_display_protection_action()
        self.display_protection_action.setToolTip(
            "On applies protection after connection; off starts the mirror directly")
        file_menu.addSeparator()
        self._menu_action(file_menu, "Save command log…", self._save_command_log, "Ctrl+Shift+S")
        self._menu_action(file_menu, "Open logs folder", self._open_logs_folder)
        file_menu.addSeparator()
        # Alt+F4 stays a Windows window-management key; binding it as an
        # application shortcut here would only shadow the native behaviour.
        self._menu_action(file_menu, "Exit", self.close)

        edit_menu = self._add_menu("&Edit")
        # Ctrl+C and Ctrl+A intentionally carry no application shortcut: they
        # already work natively in whichever field has focus, and claiming them
        # globally would break copy and select-all inside the port and key boxes.
        self._menu_action(edit_menu, "Copy log selection", self._copy_log_selection)
        self._menu_action(edit_menu, "Copy all command log", self.console_copy_all_safe, "Ctrl+Shift+C")
        self._menu_action(edit_menu, "Select all log text", self._select_all_log)
        self._menu_action(edit_menu, "Clear command log", self._clear_command_log_safe, "Ctrl+K")

        view_menu = self._add_menu("&View")
        self.sidebar_action = self._menu_action(
            view_menu, "Show sidebar", self._set_sidebar_visible, "Ctrl+B", checkable=True)
        self.sidebar_action.setChecked(True)
        self.console_action = self._menu_action(
            view_menu, "Show command log", self._set_console_visible, "Ctrl+J", checkable=True)
        self.console_action.setChecked(False)
        view_menu.addSeparator()
        self.focus_action = self._menu_action(
            view_menu, "Focus mirror", self.set_focus_mode, "F11", checkable=True)
        self.focus_action.setChecked(False)
        self._menu_action(view_menu, "Reset layout", self.reset_layout, "Ctrl+0")
        self._menu_action(view_menu, "Refresh device", self.refresh_devices, "F5")

        help_menu = self._add_menu("&Help")
        self._menu_action(help_menu, "Keyboard shortcuts", self._show_shortcuts, "Ctrl+/")
        self._menu_action(help_menu, "Diagnostics", self._show_diagnostics)
        self._menu_action(help_menu, f"About {APP_NAME}", self._show_about)

    def _add_menu(self, title):
        menu = self.menuBar().addMenu(title)
        self._menus[title] = menu
        return menu

    def _menu_action(self, menu, text, callback, shortcut=None, checkable=False):
        action = QAction(text, self)
        action.setCheckable(checkable)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            # A menu-bar action only receives its shortcut while the menu bar is
            # on screen.  Scoping to the window and registering the action on the
            # window itself keeps every accelerator live no matter which panel,
            # dialog child, or embedded mirror currently holds focus.
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            self.addAction(action)
        if checkable:
            action.triggered.connect(
                lambda checked=False, slot=callback: slot(bool(checked)))
        else:
            action.triggered.connect(
                lambda _checked=False, slot=callback: slot())
        menu.addAction(action)
        return action

    def _set_display_protection_enabled(self, enabled):
        enabled = bool(enabled)
        self._display_protection_enabled = enabled
        self._sync_display_protection_action()
        self._sync_guard_timer()
        self.settings.setValue("device/display_protection_enabled", enabled)
        self.settings.sync()
        serial = self.active_serial()

        if not enabled:
            self.status_label.setText("Display protection OFF · direct mirror")
            self._guard_generation += 1
            if serial:
                self._guard_status_by_serial[serial] = "disabled"
                self._guard_failure_reason_by_serial.pop(serial, None)
            self._set_ai_pq_status(
                "Off · mirror starts without display protection", "idle")
            self.ai_pq_status_label.setToolTip("")
            if serial and self._mirror_state in ("stopped", "ready", "failed"):
                self._set_mirror_state(
                    "ready", "Display protection off · ready to mirror")
            self.logline("[AI-PQ] Display protection disabled by the user.")
            return

        self.status_label.setText("Display protection ON · user enabled")
        self._set_ai_pq_status("Display protection enabled", "pending")
        self.logline("[AI-PQ] Display protection enabled by the user.")
        if not serial:
            return
        self._guard_generation += 1
        generation = self._guard_generation
        self._guard_status_by_serial[serial] = "protecting"
        self.enforce_ai_pq_off(
            silent=True,
            on_complete=lambda success, target=serial, token=generation:
                self._finish_initial_guard(target, token, success),
            apply_all=True,
        )

    def _sync_guard_timer(self):
        """The guard runs only while display protection is on."""
        timer = getattr(self, "ai_guard_timer", None)
        if timer is None:
            return
        if self._display_protection_enabled:
            if not timer.isActive():
                timer.start()
        elif timer.isActive():
            timer.stop()

    def _sync_display_protection_action(self):
        action = getattr(self, "display_protection_action", None)
        if action is None:
            return
        # The check mark is the state. Repeating it in the label as ON/OFF only
        # gave two things to disagree with each other.
        enabled = bool(self._display_protection_enabled)
        if action.isChecked() != enabled:
            action.blockSignals(True)
            action.setChecked(enabled)
            action.blockSignals(False)

    def _toggle_menu_bar(self):
        """Alt shows and hides the menu bar, for a taller mirror."""
        if self._focus_mode:
            return
        menu_bar = self.menuBar()
        visible = not menu_bar.isVisible()
        menu_bar.setVisible(visible)
        self.settings.setValue("ui/menu_bar_visible", visible)
        if visible:
            menu_bar.setFocus(Qt.FocusReason.MenuBarFocusReason)

    def eventFilter(self, watched, event):
        """Alt only.

        The V2.3 filter also policed mouse presses: anything it could not
        resolve to a QWidget counted as a click outside the menu and hid the
        bar.  Qt routes real mouse input through the popup's QWindow rather than
        its QWidget, so every menu click closed the menu before the action could
        fire -- which is why the View check marks never changed.  No mouse
        handling lives here now, and hiding the bar no longer disables anything:
        each action is registered on the window, so its shortcut stays live
        whether the menus are on screen or not.
        """
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Alt and not event.isAutoRepeat():
                self._alt_key_down = True
            elif self._alt_key_down:
                # Alt was part of a combination such as Alt+F4, not a bare tap.
                self._alt_key_down = False
        elif event.type() == QEvent.Type.KeyRelease:
            if event.key() == Qt.Key.Key_Alt and not event.isAutoRepeat():
                tapped = self._alt_key_down
                self._alt_key_down = False
                if tapped:
                    self._toggle_menu_bar()
        elif event.type() == QEvent.Type.ApplicationDeactivate:
            self._alt_key_down = False
        return super().eventFilter(watched, event)

    def _build_app_bar(self):
        bar = QWidget()
        bar.setObjectName("AppBar")
        bar.setFixedHeight(54)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 6, 12, 6)
        layout.setSpacing(9)

        self.sidebar_button = _role(QPushButton("☰"), "quiet")
        self.sidebar_button.setObjectName("CompactIcon")
        self.sidebar_button.setFixedSize(
            APP_BAR_CONTROL_HEIGHT, APP_BAR_CONTROL_HEIGHT)
        self.sidebar_button.setToolTip("Show or hide sidebar (Ctrl+B)")
        self.sidebar_button.clicked.connect(
            lambda _checked=False: self.toggle_sidebar())
        layout.addWidget(self.sidebar_button)

        logo = QLabel()
        logo.setObjectName("Logo")
        logo.setFixedSize(38, 38)
        pixmap = QPixmap(v1.resource_path("assets/convrse-logo.png"))
        logo.setPixmap(pixmap.scaled(
            32, 32, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)
        layout.addWidget(v1._label(APP_NAME, "CompactTitle"))
        layout.addStretch(1)

        self.status_label = v1._label("Ready", "Secondary")
        self.status_label.setMaximumWidth(280)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.status_label)

        self.device_pill = v1._label("No device", "DevicePill")
        self.device_pill.setFixedHeight(APP_BAR_CONTROL_HEIGHT)
        self.device_pill.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.device_pill)
        self.tunnel_label = v1._label("Disconnected", "TunnelPill")
        self.tunnel_label.setProperty("tone", "idle")
        self.tunnel_label.setFixedHeight(APP_BAR_CONTROL_HEIGHT)
        self.tunnel_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.tunnel_label)

        self.header_focus_button = _role(QPushButton("Full screen"), "quiet")
        self.header_focus_button.setObjectName("AppBarAction")
        self.header_focus_button.setFixedHeight(APP_BAR_CONTROL_HEIGHT)
        self.header_focus_button.clicked.connect(
            lambda _checked=False: self.toggle_focus_mode())
        layout.addWidget(self.header_focus_button)
        return bar

    def _build_sidebar(self):
        shell = QFrame()
        shell.setObjectName("SidebarV2")
        # The three-column recovery and remote grids need 328px of content, and
        # the old 302px floor clipped the right-hand column of every one of them
        # -- Refresh, Volume +, Clear Cache and Export Logs were all cut in half
        # at the default splitter position. Width now accounts for the content,
        # the layout margins and the vertical scroll bar.
        shell.setMinimumWidth(SIDEBAR_MIN_WIDTH)
        shell.setMaximumWidth(430)
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(7, 7, 4, 7)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 5, 2)
        content_layout.setSpacing(7)
        content_layout.addWidget(self._connection_card_v2())

        category_nav = QFrame()
        category_nav.setObjectName("CategoryNav")
        nav_layout = QHBoxLayout(category_nav)
        nav_layout.setContentsMargins(4, 4, 4, 4)
        nav_layout.setSpacing(3)
        self.category_buttons = []
        self.category_group = QButtonGroup(self)
        self.category_group.setExclusive(True)
        for index, title in enumerate(("Remote", "Device")):
            button = _role(QPushButton(title), "nav")
            button.setCheckable(True)
            button.setProperty("active", index == 0)
            button.clicked.connect(lambda _checked=False, page=index: self._show_category(page))
            self.category_group.addButton(button, index)
            self.category_buttons.append(button)
            nav_layout.addWidget(button, 1)
        self.category_buttons[0].setChecked(True)
        content_layout.addWidget(category_nav)

        self.category_stack = QStackedWidget()
        self.category_stack.addWidget(self._category_page(
            self._remote_card(), self._recovery_card()))
        self.category_stack.addWidget(self._category_page(
            self._health_card(), self._ai_pq_card_v2(), self._tools_card()))
        content_layout.addWidget(self.category_stack)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        shell_layout.addWidget(scroll)

        # Every sidebar action shares one control height and expands within its
        # layout cell. The Connect/Mirror pair intentionally remains the larger
        # primary action size.
        for button in shell.findChildren(QPushButton):
            if button.objectName() == "ConnectionAction":
                continue
            button.setObjectName("StandardAction")
            button.setProperty("controlSize", "standard")
            _repolish(button)
            button.setFixedHeight(CONTROL_HEIGHT)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.sidebar_scroll = scroll
        return shell

    def _connection_card_v2(self):
        card, layout = self._card("Device")

        # The key is a once-per-machine setup step, so it lives in File › Add or
        # change SSH key rather than taking permanent space here. These two stay
        # as attributes because the key state still drives their text; they are
        # only shown when there is no key yet and the operator has to act.
        self.key_status_label = v1._label("", "KeyState")
        self.key_status_label.setProperty("tone", "idle")
        self.key_status_label.setWordWrap(True)
        self.key_button = _role(QPushButton("Add SSH key"), "quiet")
        self.key_button.setObjectName("StandardAction")
        self.key_button.setFixedHeight(CONTROL_HEIGHT)
        self.key_button.clicked.connect(lambda _checked=False: self.select_pem_key())
        layout.addWidget(self.key_status_label)
        layout.addWidget(self.key_button)

        # --- Leased port: the one value that changes every single session ---
        layout.addWidget(v1._label("ADB PORT FROM THE CDM WEBSITE", "FieldLabel"))
        self.port_edit = QLineEdit()
        self.port_edit.setPlaceholderText("e.g. 17002")
        self.port_edit.setFixedHeight(CONTROL_HEIGHT)
        self.port_edit.setToolTip(
            "The website leases a different port each time a tunnel is opened. "
            "Paste whatever it shows for this device now.")
        self.port_edit.returnPressed.connect(self._toggle_tunnel)
        layout.addWidget(self.port_edit)

        # --- Route strip: local socket, leased port, and who answered -------
        self.route_label = v1._label("Not connected", "RouteStrip")
        self.route_label.setProperty("tone", "idle")
        self.route_label.setWordWrap(True)
        layout.addWidget(self.route_label)

        # Kept for the inherited operations code, which reads these adapters.
        self.ip_edit = QLineEdit(legacy.DEFAULT_IP)
        self.ip_edit.setReadOnly(True)
        self.ip_edit.hide()
        self.pem_edit = QLineEdit()
        self.pem_edit.hide()

        device_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setPlaceholderText("Select device")
        self.device_combo.setFixedHeight(CONTROL_HEIGHT)
        refresh = _role(QPushButton("Refresh"), "quiet")
        refresh.setObjectName("StandardAction")
        refresh.setFixedHeight(CONTROL_HEIGHT)
        refresh.clicked.connect(lambda _checked=False: self.refresh_devices())
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(refresh)
        layout.addLayout(device_row)

        connection_actions = EqualActionRow(spacing=6)
        self.connection_actions_row = connection_actions
        self.tunnel_button_widget = _role(QPushButton("Connect"), "primary")
        self.tunnel_button_widget.setObjectName("ConnectionAction")
        self.tunnel_button_widget.setFixedHeight(PRIMARY_ACTION_HEIGHT)
        self.tunnel_button_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tunnel_button_widget.clicked.connect(
            lambda _checked=False: self._toggle_tunnel())

        self.start_button_widget = _role(
            QPushButton("Start mirror"), "primary")
        self.start_button_widget.setObjectName("ConnectionAction")
        self.start_button_widget.setFixedHeight(PRIMARY_ACTION_HEIGHT)
        self.start_button_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.start_button_widget.setEnabled(False)
        self.start_button_widget.setToolTip("Connect a device to start mirroring")
        self.start_button_widget.clicked.connect(
            lambda _checked=False: self._toggle_mirror())
        connection_actions.set_actions(
            self.tunnel_button_widget, self.start_button_widget)
        layout.addWidget(connection_actions)

        self.mirror_status_label = v1._label("Mirror stopped", "StreamState")
        self.mirror_status_label.setProperty("tone", "idle")
        self.mirror_status_label.setWordWrap(True)
        layout.addWidget(self.mirror_status_label)

        # The leased port has to be entered every session, so the fields that
        # used to hide behind a "Connection details" disclosure now sit in the
        # open. Only the stream tuning stays collapsible.
        self.connection_details_button = _role(
            QPushButton("Stream settings  ▸"), "quiet")
        self.connection_details_button.setObjectName("StandardAction")
        self.connection_details_button.setFixedHeight(CONTROL_HEIGHT)
        self.connection_details_button.setCheckable(True)
        self.connection_details_button.clicked.connect(self._toggle_connection_details)
        layout.addWidget(self.connection_details_button)

        self.connection_details_panel = QWidget()
        detail_layout = QVBoxLayout(self.connection_details_panel)
        detail_layout.setContentsMargins(0, 1, 0, 0)
        detail_layout.setSpacing(6)

        detail_layout.addWidget(v1._label("STREAM PRESET", "FieldLabel"))
        self.profile_combo = TapSelectComboBox()
        for name, preset in STREAM_PRESETS.items():
            self.profile_combo.addItem(self._profile_combo_label(name, preset), name)
        self.profile_combo.addItem("Custom…", CUSTOM_PRESET)
        self.profile_combo.setFixedHeight(CONTROL_HEIGHT)
        selected_index = self.profile_combo.findData(self.selected_preset)
        self.profile_combo.setCurrentIndex(max(0, selected_index))
        self.profile_combo.currentIndexChanged.connect(
            self._preset_combo_changed)
        detail_layout.addWidget(self.profile_combo)

        self.custom_profile_panel = QFrame()
        self.custom_profile_panel.setObjectName("CustomProfile")
        custom_layout = QGridLayout(self.custom_profile_panel)
        custom_layout.setContentsMargins(8, 8, 8, 8)
        custom_layout.setHorizontalSpacing(6)
        custom_layout.setVerticalSpacing(5)

        custom_layout.addWidget(v1._label("RESOLUTION", "FieldLabel"), 0, 0)
        custom_layout.addWidget(v1._label("FPS", "FieldLabel"), 0, 1)
        custom_layout.addWidget(v1._label("VIDEO BITRATE", "FieldLabel"), 0, 2)
        self.custom_resolution_combo = QComboBox()
        for label, max_size in CUSTOM_RESOLUTIONS.items():
            self.custom_resolution_combo.addItem(label, max_size)
        self.custom_resolution_combo.setFixedHeight(CONTROL_HEIGHT)
        self.custom_fps_combo = QComboBox()
        for fps in CUSTOM_FPS_OPTIONS:
            self.custom_fps_combo.addItem(f"{fps} FPS", fps)
        self.custom_fps_combo.setFixedHeight(CONTROL_HEIGHT)
        self.custom_bitrate_spin = QDoubleSpinBox()
        self.custom_bitrate_spin.setRange(0.75, 12.0)
        self.custom_bitrate_spin.setDecimals(2)
        self.custom_bitrate_spin.setSingleStep(0.25)
        self.custom_bitrate_spin.setSuffix(" Mbps")
        self.custom_bitrate_spin.setFixedHeight(CONTROL_HEIGHT)
        custom_layout.addWidget(self.custom_resolution_combo, 1, 0)
        custom_layout.addWidget(self.custom_fps_combo, 1, 1)
        custom_layout.addWidget(self.custom_bitrate_spin, 1, 2)

        self.apply_custom_button = _role(QPushButton("Apply custom"), "primary")
        self.apply_custom_button.setObjectName("StandardAction")
        self.apply_custom_button.setFixedHeight(CONTROL_HEIGHT)
        self.apply_custom_button.clicked.connect(
            lambda _checked=False: self._apply_custom_profile())
        custom_layout.addWidget(self.apply_custom_button, 2, 0, 1, 3)
        self.custom_profile_panel.setVisible(self.selected_preset == CUSTOM_PRESET)
        detail_layout.addWidget(self.custom_profile_panel)

        self.connection_details_panel.hide()
        layout.addWidget(self.connection_details_panel)
        return card

    # ---------- sidebar cards: the everyday controls, then the rest ----------
    @staticmethod
    def _grid_of(buttons, columns=3):
        """Lay buttons out in a grid, returning the container widget."""
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        for index, button in enumerate(buttons):
            grid.addWidget(button, index // columns, index % columns)
        return holder

    def _disclosure(self, layout, label, panel):
        """A 'More' toggle that reveals the less-used controls.

        Nothing is removed by this; the rarely-touched controls simply stop
        competing for attention with the ones used on every device.
        """
        button = _role(QPushButton(f"{label}  ▾"), "quiet")
        button.setObjectName("StandardAction")
        button.setFixedHeight(CONTROL_HEIGHT)
        button.setCheckable(True)
        panel.hide()

        def toggled(checked):
            panel.setVisible(bool(checked))
            button.setText(f"{label}  " + ("▴" if checked else "▾"))

        button.clicked.connect(toggled)
        # The panel is added before the button so expanding grows the group
        # downwards as one block. With the button first, the extra keys appeared
        # underneath it and split the pad in half, which is what made expanding
        # look broken.
        layout.addWidget(panel)
        layout.addWidget(button)
        return button

    def _action_button(self, text, callback, role=None):
        button = QPushButton(text)
        if role:
            _role(button, role)
        button.clicked.connect(lambda _checked=False, slot=callback: slot())
        return button

    def _remote_card(self):
        card, layout = self._card("Remote control")
        key = self.send_keyevent

        layout.addWidget(self._grid_of([
            self._action_button("←  Back", lambda: key("4")),
            self._action_button("⌂  Home", lambda: key("3")),
            self._action_button("⏎  OK", lambda: key("66")),
        ]))
        # U+25C4/U+25BA rather than the U+23EE media glyphs: the media set has an
        # emoji presentation by default, so it rendered in colour next to the
        # monochrome symbols and looked like a mistake.
        layout.addWidget(self._grid_of([
            self._action_button("◄◄", lambda: key("88")),
            self._action_button("►  Play", lambda: key("85")),
            self._action_button("►►", lambda: key("87")),
        ]))

        more = QWidget()
        more_layout = QVBoxLayout(more)
        more_layout.setContentsMargins(0, 4, 0, 0)
        more_layout.setSpacing(4)
        more_layout.addWidget(self._grid_of([
            self._action_button("☰  Menu", lambda: key("82")),
            self._action_button("⏻  Power", lambda: key("26")),
            self._action_button("☀  Wake", lambda: key("224")),
            self._action_button("☾  Sleep", lambda: key("223")),
            self._action_button("Vol  −", lambda: key("25")),
            self._action_button("⊘  Mute", lambda: key("164")),
            self._action_button("Vol  +", lambda: key("24")),
            self._action_button("⛶  Shot", self.screenshot),
            self._action_button("⚙  Settings", self.open_device_settings),
        ]))
        custom = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("Key code")
        self.key_edit.setMaximumWidth(100)
        custom.addWidget(self.key_edit)
        custom.addWidget(
            self._action_button("Send key code", self.send_custom_keyevent), 1)
        more_layout.addLayout(custom)
        self._disclosure(layout, "More controls", more)
        return card

    def _recovery_card(self):
        card, layout = self._card("App recovery")
        self.current_app_label = v1._label(
            "Foreground app: detected automatically", "Package")
        self.current_app_label.setWordWrap(True)
        layout.addWidget(self.current_app_label)

        layout.addWidget(self._grid_of([
            self._action_button("Restart App", self.restart_current, "primary"),
            self._action_button("Force Stop", self.force_stop_current),
        ], columns=2))

        more = QWidget()
        more_layout = QVBoxLayout(more)
        more_layout.setContentsMargins(0, 4, 0, 0)
        more_layout.addWidget(self._grid_of([
            self._action_button("Clear Cache", self.clear_current_cache),
            self._action_button("Close Background", self.close_background_apps),
            self._action_button("Export Logs", self.save_recent_logs),
            # Clear Data wipes the app's pairing and login state on the device,
            # so it stays behind the disclosure with its danger styling.
            self._action_button("Clear Data", self.clear_current_data, "danger"),
        ], columns=2))
        self._disclosure(layout, "More recovery", more)
        return card

    def _health_card(self):
        """The morning check: one device, one verdict, one click to act on it."""
        card, layout = self._card("Morning check")

        self.health_status_label = v1._label(
            "Connect a device and run the check", "AiStatus")
        self.health_status_label.setProperty("tone", "idle")
        self.health_status_label.setWordWrap(True)
        layout.addWidget(self.health_status_label)

        self.health_detail_label = v1._label("", "Tertiary")
        self.health_detail_label.setWordWrap(True)
        self.health_detail_label.hide()
        layout.addWidget(self.health_detail_label)

        self.health_run_button = _role(QPushButton("Run check"), "primary")
        self.health_run_button.setToolTip(
            "Look at what this device is doing: which app is in front, whether "
            "the picture is moving, and whether a dialog is covering it")
        self.health_run_button.clicked.connect(
            lambda _checked=False: self.run_health_check())
        layout.addWidget(self.health_run_button)

        # Shown only when the check found something, so the panel stays quiet
        # on a healthy device.
        self.health_actions = QWidget()
        action_layout = QHBoxLayout(self.health_actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(6)
        self.health_mirror_button = _role(QPushButton("Open mirror"), "warning")
        self.health_mirror_button.setToolTip(
            "Take a look yourself on this already-verified session")
        self.health_mirror_button.clicked.connect(
            lambda _checked=False: self._health_open_mirror())
        self.health_confirm_button = _role(QPushButton("This is correct"), "quiet")
        self.health_confirm_button.setToolTip(
            "Remember the app now in front as the one expected on this device")
        self.health_confirm_button.clicked.connect(
            lambda _checked=False: self._health_confirm_baseline())
        action_layout.addWidget(self.health_mirror_button)
        action_layout.addWidget(self.health_confirm_button)
        self.health_actions.hide()
        layout.addWidget(self.health_actions)
        return card

    def _health_expected_package(self, serial):
        return self.settings.value(f"health/expected/{serial}", "", str)

    def _set_health_status(self, text, tone="idle", detail=""):
        self.health_status_label.setText(text)
        self.health_status_label.setProperty("tone", tone)
        _repolish(self.health_status_label)
        self.health_detail_label.setText(detail)
        self.health_detail_label.setVisible(bool(detail))

    def run_health_check(self):
        address = self.active_serial()
        if not address:
            QMessageBox.information(
                self, APP_NAME,
                "Connect a device first, then run the check.")
            return
        identity = self._identity_by_address.get(address)
        serial = identity.serial if identity else address
        expected = self._health_expected_package(serial)

        self.health_run_button.setEnabled(False)
        self.health_actions.hide()
        self._set_health_status("Looking at the device…", "pending")

        def job():
            runner = health.AdbRunner(
                address, legacy.adb_command,
                screenshot_fn=lambda: self._grab_frame(address))
            try:
                result = health.run_health_check(
                    runner, expected_package=expected, with_cleanup=True)
            except Exception as exc:                       # pragma: no cover
                self.ui(self._finish_health_check, None, str(exc))
                return
            self.ui(self._finish_health_check, result, "")

        self.worker(job)

    def _grab_frame(self, address):
        """One screenshot, decoded in memory. Returns None if unavailable."""
        try:
            from io import BytesIO
            from PIL import Image
        except ImportError:                                 # pragma: no cover
            return None
        try:
            code, raw, _stderr = self._adb_binary_command(
                address, "exec-out", "screencap", "-p", timeout=30)
        except Exception:
            return None
        if code != 0 or not raw:
            return None
        try:
            return Image.open(BytesIO(raw)).convert("RGB")
        except Exception:
            # A device that refuses screencap still gets judged on the other
            # signals; analyze_frames reports SCREENSHOT_UNAVAILABLE.
            return None

    def _finish_health_check(self, result, error):
        self.health_run_button.setEnabled(True)
        if error or result is None:
            self._set_health_status(
                "The check could not finish", "error", error)
            self.logline(f"[Health] {error}")
            return

        self._last_health_result = result
        tone = {
            health.HEALTHY: "online",
            health.UNCONFIRMED: "pending",
            health.NEEDS_ATTENTION: "error",
            health.OFFLINE: "error",
        }.get(result.verdict, "idle")
        detail = "\n".join(f"• {reason}" for reason in result.reasons)
        self._set_health_status(f"{result.verdict} · {result.summary()}", tone, detail)
        self.logline(f"[Health] {result.verdict}: {result.summary()}")
        for reason in result.reasons:
            self.logline(f"[Health]   {reason}")
        self.logline("[Health] " + result.daily_check_detail().replace("\n", " | "))

        # Offer the mirror when something is wrong, and the confirm action when
        # the device looks fine but nobody has said what belongs in front.
        show_mirror = result.needs_operator
        show_confirm = (
            result.verdict == health.UNCONFIRMED and bool(result.foreground_package))
        self.health_mirror_button.setVisible(show_mirror)
        self.health_confirm_button.setVisible(show_confirm)
        self.health_actions.setVisible(show_mirror or show_confirm)

    def _health_open_mirror(self):
        """Hand the operator the screen on the session already verified."""
        self._health_recheck_pending = True
        if self._mirror_state != "running":
            self.start_scrcpy()

    def _health_confirm_baseline(self):
        result = getattr(self, "_last_health_result", None)
        if result is None or not result.foreground_package:
            return
        serial = result.serial or result.address
        self.settings.setValue(
            f"health/expected/{serial}", result.foreground_package)
        self.settings.sync()
        self.logline(
            f"[Health] {result.foreground_package} recorded as expected on "
            f"{serial}.")
        self.run_health_check()

    def _tools_card(self):
        card, layout = self._card("Tools")
        layout.addWidget(self._grid_of([
            self._action_button("Open CleanUp", self.open_cleanup_app),
            self._action_button("Install APK", self.install_apk),
        ], columns=2))

        more = QWidget()
        more_layout = QVBoxLayout(more)
        more_layout.setContentsMargins(0, 4, 0, 0)
        more_layout.setSpacing(4)
        more_layout.addWidget(self._grid_of([
            self._action_button("Open Store", self.open_convrse_store),
            self._action_button("Launch Claude", self.run_claude),
        ], columns=2))
        self.capture_button_widget = _role(
            QPushButton("Start diagnostic session"), "primary")
        self.capture_button_widget.clicked.connect(
            lambda _checked=False: self.toggle_debug_capture())
        more_layout.addWidget(self.capture_button_widget)
        self._disclosure(layout, "More tools", more)
        return card

    def _ai_pq_card_v2(self):
        card, layout = self._card("Display processing")
        self.ai_pq_status_label = v1._label("Waiting for a device", "AiStatus")
        self.ai_pq_status_label.setProperty("tone", "idle")
        self.ai_pq_status_label.setWordWrap(True)
        layout.addWidget(self.ai_pq_status_label)

        self.ai_controls_button = _role(QPushButton("7 controls  ▸"), "quiet")
        self.ai_controls_button.setCheckable(True)
        self.ai_controls_button.clicked.connect(self._toggle_ai_controls)
        layout.addWidget(self.ai_controls_button)

        self.ai_controls_panel = QWidget()
        controls_layout = QVBoxLayout(self.ai_controls_panel)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(5)
        self.ai_toggle_labels = OrderedDict()
        for toggle_spec in AI_DISPLAY_TOGGLES:
            toggle_name = toggle_spec.name
            row = QFrame()
            row.setObjectName("ToggleRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 5, 8, 5)
            name_label = v1._label(toggle_name, "ToggleName")
            value_label = v1._label("Checking…", "ToggleValue")
            value_label.setProperty("tone", "idle")
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(name_label)
            row_layout.addStretch(1)
            row_layout.addWidget(value_label)
            controls_layout.addWidget(row)
            self.ai_toggle_labels[toggle_name] = value_label
        self.ai_controls_panel.hide()
        layout.addWidget(self.ai_controls_panel)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        verify = _role(QPushButton("Verify now"), "quiet")
        verify.clicked.connect(
            lambda _checked=False: self.verify_ai_pq_state())
        actions.addWidget(verify)
        # Verifying only ever reported a mismatch and left it in place, so a
        # device that drifted stayed drifted until someone noticed. Correcting
        # it is a single command over the ADB root shell we already hold.
        self.ai_fix_button = _role(QPushButton("Turn processing off"), "primary")
        self.ai_fix_button.setToolTip(
            "Set every display-processing property back to off on this device")
        self.ai_fix_button.clicked.connect(
            lambda _checked=False: self._correct_display_processing())
        actions.addWidget(self.ai_fix_button)
        layout.addLayout(actions)
        return card

    def _correct_display_processing(self):
        """Apply the off baseline now, whatever the auto-guard toggle says."""
        serial = self.active_serial()
        if not serial:
            QMessageBox.information(
                self, APP_NAME, "Connect a device first.")
            return
        self.ai_fix_button.setEnabled(False)
        self._set_ai_pq_status("Turning display processing off…", "pending")
        self.enforce_ai_pq_off(
            silent=True,
            on_complete=lambda success: self._finish_display_correction(success),
            apply_all=True,
            force=True,
        )

    def _finish_display_correction(self, success):
        if self._closing:
            return
        self.ai_fix_button.setEnabled(True)
        if success:
            self.logline("[AI-PQ] Display processing corrected on request.")
        self.verify_ai_pq_state()

    def _toggle_ai_controls(self, checked):
        checked = bool(checked)
        self.ai_controls_panel.setVisible(checked)
        self.ai_controls_button.setText(
            "7 controls  ▾" if checked else "7 controls  ▸")

    def _build_workspace(self):
        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(6)

        work = QWidget()
        work_layout = QVBoxLayout(work)
        work_layout.setContentsMargins(0, 0, 0, 0)
        work_layout.setSpacing(0)

        # Compatibility surfaces used by inherited diagnostics remain detached
        # from the layout so no elapsed timer or redundant stream toolbar is
        # rendered above the mirror.
        self.stream_metrics_label = v1._label(self._target_metric_text())
        self.session_status_label = v1._label("Diagnostics idle", "Tertiary")
        self.session_detail_label = v1._label("", "Tertiary")

        self.mirror_frame = AspectMirrorHost()
        self.mirror_frame.doubleClicked.connect(self.toggle_focus_mode)
        work_layout.addWidget(self.mirror_frame, 1)
        self.workspace_splitter.addWidget(work)

        self.console = V2CommandConsole()
        self.console.toggleRequested.connect(self.toggle_console)
        self.workspace_splitter.addWidget(self.console)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 0)
        self.workspace_splitter.setSizes([700, 22])
        return self.workspace_splitter

    def _install_adapters(self):
        self.pem_var = v1.ValueAdapter(self.pem_edit.text, self.pem_edit.setText)
        self.port_var = v1.ValueAdapter(self.port_edit.text, self.port_edit.setText)
        self.ip_var = v1.ValueAdapter(self.ip_edit.text, self.ip_edit.setText)
        self.key_var = v1.ValueAdapter(self.key_edit.text, self.key_edit.setText)
        # The combo shows a device name but every consumer expects the ADB
        # address, so the adapter reads item data rather than the visible text.
        self.device_var = v1.ValueAdapter(
            self._current_device_serial, self._set_device)
        self.profile_var = PresetAdapter(self)
        self.current_app_var = v1.ValueAdapter(
            self.current_app_label.text, self.current_app_label.setText)
        self.status_var = v1.ValueAdapter(self.status_label.text, self.status_label.setText)
        self.session_status_var = v1.ValueAdapter(
            self.session_status_label.text, self.session_status_label.setText)
        self.session_detail_var = v1.ValueAdapter(
            self.session_detail_label.text, self.session_detail_label.setText)
        self.tunnel_var = v1.ValueAdapter(self.tunnel_label.text, self.tunnel_label.setText)
        self.tunnel_btn = v1.ButtonAdapter(self.tunnel_button_widget)
        self.capture_btn = CaptureButtonAdapter(self.capture_button_widget)
        self.start_btn = v1.ButtonAdapter(self.start_button_widget)
        self.device_box = v1.ComboAdapter(self.device_combo)
        self.device_combo.currentIndexChanged.connect(
            lambda _index: self._device_changed(self._current_device_serial()))

        self._migrate_legacy_key()
        self._sync_key_state()
        if conn.has_stored_key():
            self.pem_edit.setText(str(conn.stored_key_path()))
        # The leased port is deliberately not restored. Reusing yesterday's
        # number is exactly how an operator ends up attached to whichever device
        # the gateway has since put there, which may be a colleague's.
        self.port_edit.clear()
        self._load_custom_profile()

        self.focus_escape_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.focus_escape_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.focus_escape_shortcut.activated.connect(self._escape_focus)
        self.console_focus_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self.console_focus_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.console_focus_shortcut.activated.connect(self._focus_command_log)

    # ---------- menus and layout state ----------
    @staticmethod
    def _bool_value(value, default=True):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _restore_layout(self):
        main_state = self.settings.value("ui/main_splitter")
        workspace_state = self.settings.value("ui/workspace_splitter")
        if not main_state or not self.main_splitter.restoreState(main_state):
            self.main_splitter.setSizes([SIDEBAR_MIN_WIDTH, 1200])
        else:
            self._last_main_splitter_state = main_state
        if not workspace_state or not self.workspace_splitter.restoreState(workspace_state):
            self.workspace_splitter.setSizes([700, 170])
        else:
            self._last_workspace_splitter_state = workspace_state

        sidebar_visible = self._bool_value(
            self.settings.value("ui/sidebar_visible", True), True)
        console_visible = self._bool_value(
            self.settings.value("ui/console_visible_minimal", False), False)
        self._set_sidebar_visible(sidebar_visible, persist=False)
        self._set_console_visible(console_visible, persist=False)
        details_visible = self._bool_value(
            self.settings.value("ui/connection_details", False), False)
        self.connection_details_button.setChecked(details_visible)
        self.connection_details_panel.setVisible(details_visible)
        self.connection_details_button.setText(
            "Stream settings  ▾" if details_visible else "Stream settings  ▸")
        category = max(0, min(1, self.settings.value("ui/category", 0, int)))
        self._show_category(category)
        self._set_mirror_state("stopped")

    def _save_layout(self):
        # The key lives in its own store; the leased port is per-session and is
        # intentionally never carried over to the next launch.
        self.settings.setValue("stream/preset", self.selected_preset)
        self.settings.setValue(
            "device/display_protection_enabled",
            self._display_protection_enabled,
        )
        self.settings.setValue(
            "ui/connection_details", self.connection_details_button.isChecked())
        self.settings.setValue("ui/category", self.category_stack.currentIndex())

        if self._focus_mode:
            main_state = self._focus_restore.get("main") or self._last_main_splitter_state
            workspace_state = (
                self._focus_restore.get("workspace")
                or self._last_workspace_splitter_state)
            sidebar_visible = self._focus_restore.get("sidebar", True)
            console_visible = self._focus_restore.get("console", True)
        else:
            main_state = (
                self.main_splitter.saveState()
                if self.sidebar.isVisible() else self._last_main_splitter_state)
            workspace_state = (
                self.workspace_splitter.saveState()
                if not self._console_collapsed else self._last_workspace_splitter_state)
            sidebar_visible = self.sidebar.isVisible()
            console_visible = not self._console_collapsed
        if main_state:
            self.settings.setValue("ui/main_splitter", main_state)
        if workspace_state:
            self.settings.setValue("ui/workspace_splitter", workspace_state)
        self.settings.setValue("ui/sidebar_visible", sidebar_visible)
        self.settings.setValue("ui/console_visible_minimal", console_visible)
        self.settings.sync()

    def _toggle_connection_details(self, checked):
        self.connection_details_panel.setVisible(bool(checked))
        self.connection_details_button.setText(
            "Stream settings  ▾" if checked else "Stream settings  ▸")
        self.settings.setValue("ui/connection_details", bool(checked))

    def toggle_sidebar(self):
        self._set_sidebar_visible(not self.sidebar.isVisible())

    def _set_sidebar_visible(self, visible, persist=True):
        visible = bool(visible)
        was_visible = self.sidebar.isVisible()
        if was_visible and not visible and not self._focus_mode:
            self._last_main_splitter_state = self.main_splitter.saveState()
        self.sidebar.setVisible(visible)
        if visible and not was_visible and self._last_main_splitter_state:
            QTimer.singleShot(
                0, lambda state=self._last_main_splitter_state:
                self.main_splitter.restoreState(state))
        self.sidebar_action.setChecked(visible)
        self.sidebar_button.setText("☰" if visible else "☷")
        if persist and not self._focus_mode:
            self.settings.setValue("ui/sidebar_visible", visible)

    def toggle_console(self):
        self._set_console_visible(self._console_collapsed)

    def _set_console_visible(self, visible, persist=True):
        visible = bool(visible)
        if visible and self._console_collapsed:
            self.console.set_collapsed_visual(False)
            total = max(sum(self._saved_console_sizes), self.workspace_splitter.height())
            console_height = max(150, self._saved_console_sizes[-1])
            self.workspace_splitter.setSizes([max(300, total - console_height), console_height])
            self._console_collapsed = False
            if self._last_workspace_splitter_state:
                QTimer.singleShot(
                    0, lambda state=self._last_workspace_splitter_state:
                    self.workspace_splitter.restoreState(state))
        elif not visible and not self._console_collapsed:
            sizes = self.workspace_splitter.sizes()
            if len(sizes) == 2 and sizes[1] > 45:
                self._saved_console_sizes = sizes
                self._last_workspace_splitter_state = self.workspace_splitter.saveState()
            self.console.set_collapsed_visual(True)
            total = max(sum(sizes), self.workspace_splitter.height())
            self.workspace_splitter.setSizes([max(300, total - 22), 22])
            self._console_collapsed = True
        elif not visible:
            self.console.set_collapsed_visual(True)
            sizes = self.workspace_splitter.sizes()
            total = max(sum(sizes), self.workspace_splitter.height())
            self.workspace_splitter.setSizes([max(300, total - 22), 22])
        self.console_action.setChecked(not self._console_collapsed)
        if persist and not self._focus_mode:
            self.settings.setValue(
                "ui/console_visible_minimal", not self._console_collapsed)

    def reset_layout(self):
        if self._focus_mode:
            self.exit_focus_mode()
        self._set_sidebar_visible(True)
        self._set_console_visible(False)
        self.main_splitter.setSizes([SIDEBAR_MIN_WIDTH, max(700, self.width() - SIDEBAR_MIN_WIDTH)])
        self.workspace_splitter.setSizes([max(430, self.height() - 102), 22])
        self._last_main_splitter_state = self.main_splitter.saveState()
        self._last_workspace_splitter_state = self.workspace_splitter.saveState()
        self.select_preset(DEFAULT_PRESET, restart=False)
        self.status_var.set("Layout reset")

    def _copy_log_selection(self):
        self.console.output.copy()

    def console_copy_all_safe(self):
        if hasattr(self, "console"):
            self.console.copy_all()

    def _select_all_log(self):
        self.console.output.setFocus()
        self.console.output.selectAll()

    def _clear_command_log_safe(self):
        if hasattr(self, "console"):
            self.console.clear()

    def _save_command_log(self):
        if hasattr(self, "console"):
            self.console.export_log()

    def _open_logs_folder(self):
        folder = os.path.join(os.path.expanduser("~"), "Documents", "CDC Sessions")
        os.makedirs(folder, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            v1.QtMessageBoxAdapter.showerror(APP_NAME, f"Could not open logs folder:\n{exc}")

    def _show_shortcuts(self):
        QMessageBox.information(
            self, "Keyboard shortcuts",
            "F11\tFocus mirror\n"
            "Esc\tExit focus mode\n"
            "Ctrl+B\tShow or hide sidebar\n"
            "Ctrl+J\tShow or hide command log\n"
            "Ctrl+L\tFocus command log\n"
            "Ctrl+K\tClear command log\n"
            "F5\tRefresh devices\n"
            "Ctrl+0\tReset layout\n"
            "Ctrl+O\tAdd or change SSH key\n"
            "Ctrl+I\tInstall APK\n"
            "Ctrl+Shift+S\tSave command log")

    def _show_diagnostics(self):
        self._set_sidebar_visible(True)
        self._show_category(1)
        self._set_console_visible(True)
        self.console.output.setFocus()

    def _show_about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b><br>"
            f"Version {APP_VERSION}<br><br>"
            "Mirror-first Android operations, recovery, and diagnostics "
            "workspace for the Convrse device fleet.<br><br>"
            f"Gateway&nbsp;&nbsp;{legacy.SSH_HOST}<br>"
            f"Runtime&nbsp;&nbsp;PySide6 {_qt_version()} · scrcpy · ADB · OpenSSH<br>"
            f"Settings&nbsp;&nbsp;{self.settings.fileName()}")

    def _focus_command_log(self):
        if self._focus_mode:
            self.exit_focus_mode()
        self._set_console_visible(True)
        self.console.output.setFocus()

    # ---------- connection and selection state ----------
    @staticmethod
    def _bitrate_label(bitrate_bps):
        value = float(bitrate_bps) / 1_000_000
        return f"{value:g} Mbps"

    def _current_device_serial(self):
        if hasattr(self, "device_combo"):
            # Item data is the ADB address; the visible text is the device name.
            return (self.device_combo.currentData() or "").strip()
        return ""

    def _stream_capabilities_for(self, serial=None):
        serial = (serial or self._current_device_serial() or "").strip()
        return self._stream_capabilities.get(serial, DeviceStreamCapabilities())

    @staticmethod
    def _limited_resolution_label(max_size, capabilities, fallback="Device native"):
        width = capabilities.width
        height = capabilities.height
        if not width or not height:
            return fallback
        if max_size <= 0 or max(width, height) <= max_size:
            return f"{width} \u00d7 {height}"
        scale = float(max_size) / max(width, height)
        limited_width = max(2, int(round((width * scale) / 2.0)) * 2)
        limited_height = max(2, int(round((height * scale) / 2.0)) * 2)
        return f"{limited_width} \u00d7 {limited_height}"

    def _profile_combo_label(self, name, preset):
        capabilities = self._stream_capabilities_for()
        resolution = self._limited_resolution_label(
            preset.max_size, capabilities, preset.resolution)
        max_fps = preset.max_fps
        if max_fps is None:
            refresh = capabilities.refresh_hz or 60.0
            max_fps = max(15, min(60, int(round(refresh))))
        return (
            f"{name} · {resolution} · {max_fps} FPS · "
            f"{self._bitrate_label(preset.bitrate_bps)} · H.264"
        )

    def _refresh_profile_combo_labels(self):
        for index, (name, preset) in enumerate(STREAM_PRESETS.items()):
            self.profile_combo.setItemText(
                index, self._profile_combo_label(name, preset))

    @staticmethod
    def _custom_device_key(serial):
        identity = (serial or "default").strip() or "default"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    def _custom_settings_prefix(self, serial=None):
        return f"stream/custom/{self._custom_device_key(serial or self._current_device_serial())}"

    def _load_custom_profile(self, serial=None):
        if not hasattr(self, "custom_resolution_combo"):
            return
        prefix = self._custom_settings_prefix(serial)
        max_size = self.settings.value(
            f"{prefix}/max_size", DEFAULT_CUSTOM_MAX_SIZE, int)
        fps = self.settings.value(f"{prefix}/fps", DEFAULT_CUSTOM_FPS, int)
        bitrate = self.settings.value(
            f"{prefix}/bitrate_mbps", DEFAULT_CUSTOM_BITRATE_MBPS, float)
        if max_size not in CUSTOM_RESOLUTIONS.values():
            max_size = DEFAULT_CUSTOM_MAX_SIZE
        if fps not in CUSTOM_FPS_OPTIONS:
            fps = DEFAULT_CUSTOM_FPS
        bitrate = max(0.75, min(12.0, float(bitrate)))
        resolution_index = self.custom_resolution_combo.findData(max_size)
        fps_index = self.custom_fps_combo.findData(fps)
        self.custom_resolution_combo.setCurrentIndex(max(0, resolution_index))
        self.custom_fps_combo.setCurrentIndex(max(0, fps_index))
        self.custom_bitrate_spin.setValue(bitrate)

    def _save_custom_profile(self, serial=None):
        prefix = self._custom_settings_prefix(serial)
        self.settings.setValue(
            f"{prefix}/max_size", int(self.custom_resolution_combo.currentData()))
        self.settings.setValue(
            f"{prefix}/fps", int(self.custom_fps_combo.currentData()))
        self.settings.setValue(
            f"{prefix}/bitrate_mbps", float(self.custom_bitrate_spin.value()))

    def _custom_preset(self):
        max_size = int(self.custom_resolution_combo.currentData())
        fallback = self.custom_resolution_combo.currentText()
        return StreamPreset(
            resolution=fallback,
            max_size=max_size,
            bitrate_bps=int(round(self.custom_bitrate_spin.value() * 1_000_000)),
            max_fps=int(self.custom_fps_combo.currentData()),
        )

    def _apply_custom_profile(self):
        self._save_custom_profile()
        was_active = self.selected_preset == CUSTOM_PRESET
        self.selected_preset = CUSTOM_PRESET
        self.settings.setValue("stream/preset", CUSTOM_PRESET)
        custom_index = self.profile_combo.findData(CUSTOM_PRESET)
        if custom_index >= 0 and self.profile_combo.currentIndex() != custom_index:
            blocked = self.profile_combo.blockSignals(True)
            self.profile_combo.setCurrentIndex(custom_index)
            self.profile_combo.blockSignals(blocked)
        self.custom_profile_panel.show()
        self._update_stream_metrics_label()
        if self._mirror_state in ("running", "starting"):
            self._pending_restart = True
            self._set_mirror_state("restarting", "Applying Custom stream…")
            self._terminate_scrcpy()
        else:
            verb = "updated" if was_active else "selected"
            self.mirror_status_label.setText(f"Custom {verb} · ready to start")

    def _toggle_tunnel(self):
        if ((self.ssh_proc and self.ssh_proc.poll() is None)
                or self._external_tunnel):
            self.disconnect_tunnel()
        else:
            self.connect_tunnel()

    # ---------- SSH key: imported once, then never asked for again ----------
    def _migrate_legacy_key(self):
        """Adopt a key chosen by an earlier build so nobody re-imports by hand."""
        if conn.has_stored_key():
            return
        candidates = []
        saved = self.settings.value("connection/pem", "", str)
        if saved:
            candidates.append(saved)
        candidates.append(
            os.path.join(os.path.expanduser("~"), "Downloads", "cdm-key.pem"))
        for candidate in candidates:
            try:
                if candidate and os.path.isfile(candidate):
                    conn.import_key_file(candidate)
                    self.logline(
                        f"[Key] Imported the existing key from {candidate} into "
                        "private storage. You will not be asked for it again.")
                    return
            except conn.KeyImportError:
                continue

    def _sync_key_state(self):
        """Show the key prompt only while there is no key to use.

        Once imported it never needs attention again, so it stops occupying the
        sidebar and lives in File > Add or change SSH key instead.
        """
        has_key = conn.has_stored_key()
        self.key_status_label.setVisible(not has_key)
        self.key_button.setVisible(not has_key)
        if not has_key:
            self.key_status_label.setText(
                "SSH key not set — add it once to connect")
            self.key_status_label.setProperty("tone", "error")
            self.key_button.setText("Add SSH key")
            _repolish(self.key_status_label)
        return has_key

    def select_pem_key(self):
        """Import the key by paste or file. Replaces the old browse-every-time."""
        dialog = KeyImportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            if dialog.mode == "file":
                path = conn.import_key_file(dialog.file_path)
            else:
                path = conn.import_key_text(dialog.key_text)
        except conn.KeyImportError as exc:
            QMessageBox.warning(self, "SSH key", str(exc))
            return
        self.settings.setValue("connection/pem", str(path))
        self.settings.sync()
        self._sync_key_state()
        self.logline(f"[Key] Stored at {path} with access limited to this account.")
        self.status_var.set("SSH key saved")

    def _require_key(self):
        if conn.has_stored_key():
            return str(conn.stored_key_path())
        QMessageBox.information(
            self, "SSH key needed",
            "Add the cdm.convrse.ai SSH key once and this computer will not ask "
            "again.\n\nYou can paste the key text or pick the .pem file.")
        self.select_pem_key()
        return str(conn.stored_key_path()) if conn.has_stored_key() else None

    # ---------- tunnel: leased remote port, private local socket ----------
    def _set_route_state(self, text, tone="idle"):
        self.route_label.setText(text)
        self.route_label.setProperty("tone", tone)
        _repolish(self.route_label)

    def connect_tunnel(self):
        pem_path = self._require_key()
        if not pem_path:
            return

        remote_port = conn.parse_endpoint(self.port_edit.text())
        if remote_port is None:
            QMessageBox.warning(
                self, APP_NAME,
                "Enter the ADB port that the CDM website shows for this device.\n\n"
                "The website leases a different port every time a tunnel is "
                "opened, so this value changes between sessions and between "
                "colleagues. Paste whatever it shows now.")
            self.port_edit.setFocus()
            self.port_edit.selectAll()
            return
        self.port_edit.setText(str(remote_port))

        ssh_executable = shutil.which("ssh.exe") or shutil.which("ssh")
        if not ssh_executable:
            QMessageBox.critical(
                self, APP_NAME,
                "Windows OpenSSH was not found.\n\n"
                "Install it from Settings › System › Optional features › "
                "OpenSSH Client, then try again.")
            return

        local_port = conn.find_free_local_port()
        self._pending_local_port = local_port
        self._pending_remote_port = remote_port
        target = f"{legacy.DEFAULT_IP}:{local_port}"

        self.ip_edit.setText(f"{legacy.DEFAULT_IP}:{local_port}")
        self._set_route_state(
            conn.describe_route(local_port, remote_port, legacy.SSH_HOST), "pending")
        self.status_var.set(f"Opening tunnel to leased port {remote_port} …")
        self._set_tunnel_state("Connecting…", "pending")

        def job():
            command = conn.build_ssh_command(
                ssh_executable, pem_path, local_port, remote_port, legacy.SSH_HOST)
            self.logline("[SSH] " + legacy.command_text(command))
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    creationflags=legacy._NO_WINDOW,
                )
            except OSError as exc:
                self._fail_tunnel(f"Could not start SSH: {exc}")
                return

            self.ssh_proc = proc
            self.ssh_port = local_port
            self.ssh_remote_port = remote_port
            self.ssh_pem = pem_path
            threading.Thread(
                target=self._watch_ssh_output, args=(proc,), daemon=True).start()

            for _ in range(40):
                if proc.poll() is not None:
                    break
                if conn.is_port_in_use(local_port):
                    break
                time.sleep(0.25)
            else:
                self._fail_tunnel(
                    f"The forward to leased port {remote_port} never came up.")
                return
            if proc.poll() is not None:
                self._fail_tunnel(
                    "SSH exited before the tunnel opened. If it reports "
                    "'Permission denied (publickey)', the key needs replacing.")
                return

            self.ui(
                self._set_route_state,
                conn.describe_route(local_port, remote_port, legacy.SSH_HOST),
                "online")
            self._connect_adb_target(target)

        self.worker(job)

    def _fail_tunnel(self, message):
        self.logline("[SSH] " + message)
        self.ui(self._set_tunnel_state, "Connection failed", "error")
        self.ui(self._set_route_state, message, "error")
        self.ui(self.status_var.set, "Tunnel failed — see command log")
        proc = self.ssh_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.ssh_proc = None

    def _connect_adb_target(self, target):
        """Attach ADB, then prove which device actually answered."""
        result = legacy.adb_command("connect", target, timeout=40)
        self.logline(f"[ADB] connect {target}: {result[1] or 'no response'}")
        output = (result[1] or "").lower()
        if result[0] != 0 or not ("connected" in output or "already" in output):
            # The forward opened, so SSH and the key are fine and the gateway
            # accepted the connection.  The device behind it did not answer.
            # In the field this is nearly always an expired lease: the tunnel
            # auto-expires, the local socket keeps accepting, and only the ADB
            # handshake reveals that nothing is on the other end.
            self._fail_tunnel(
                f"The tunnel to port {self.ssh_remote_port} opened, but the "
                "device did not answer.\n\n"
                "This usually means the tunnel has expired. Open a fresh one "
                "for this device on the CDM website and use the new port it "
                "shows — it will not be the same number.")
            return

        identity = self._read_device_identity(target)
        if not identity:
            self.logline(
                "[ADB] The device did not answer a property query. Treating the "
                "tunnel as up but unverified.")
        else:
            self._connected_identity = identity
            self.logline(
                f"[Device] {identity.label()}"
                + (f" · {identity.model}" if identity.model else "")
                + (f" · Android {identity.android}" if identity.android else ""))
            self.ui(self._show_identity, identity)

        self.ui(
            self._set_tunnel_state,
            f"Live on {self.ssh_remote_port}", "online")
        self.ui(self.status_var.set, "Connected")
        self._refresh_devices_job(preferred=target)
        # ADB can take a few seconds to promote a fresh transport from offline
        # to device. Poll for it instead of leaving the operator to press
        # Refresh until something shows up.
        self.ui(self._start_device_watch, target)

    # ---------- device list: names, not loopback addresses ----------
    def _refresh_devices_job(self, preferred=None):
        """List devices by who they are rather than by their ADB address.

        ADB addresses a tunnelled device as 127.0.0.1:<local port>, which says
        nothing about which device it is and changes every session. The list now
        shows the serial the CDM website shows, keeping the address as item data
        so everything downstream still receives what ADB expects.
        """
        rc, output, _elapsed = legacy.adb_command("devices", timeout=20)
        addresses = []
        if rc == 0:
            for line in output.splitlines()[1:]:
                fields = line.strip().split()
                if len(fields) >= 2 and fields[1] == "device":
                    addresses.append(fields[0])

        for address in addresses:
            if address not in self._identity_by_address:
                identity = self._read_device_identity(address)
                if identity:
                    self._identity_by_address[address] = identity
        # Drop identities for transports that have gone away, so a stale entry
        # cannot keep a name alive after its tunnel closed.
        for known in list(self._identity_by_address):
            if known not in addresses:
                self._identity_by_address.pop(known, None)

        def update():
            previous = self._current_device_serial()
            self.devices = addresses
            combo = self.device_combo
            combo.blockSignals(True)
            combo.clear()
            for address in addresses:
                identity = self._identity_by_address.get(address)
                combo.addItem(identity.label() if identity else address, address)
                if identity:
                    combo.setItemData(
                        combo.count() - 1, identity.details(),
                        Qt.ItemDataRole.ToolTipRole)
            combo.blockSignals(False)

            target = None
            for candidate in (preferred, previous):
                if candidate and candidate in addresses:
                    target = candidate
                    break
            if target is None and addresses:
                target = addresses[0]
            self._set_device(target or "")

            if addresses:
                self.status_var.set(f"{len(addresses)} device(s) connected")
            else:
                self.status_var.set("No authorized devices found")
                if output and rc != 0:
                    self.logline("[Devices] " + output)

        self.ui(update)

    def _set_device(self, value):
        """Select by ADB address, which is the item data rather than the text."""
        combo = self.device_combo
        index = combo.findData(value) if value else -1
        if index != combo.currentIndex():
            combo.setCurrentIndex(index)
        else:
            # findData found the row already showing; still announce it so the
            # rest of the window can react to a reconnect of the same device.
            self._device_changed(value or "")

    def _show_identity(self, identity):
        """Put the serial where it can be compared against the CDM website."""
        self.device_pill.setText(identity.label())
        self.device_pill.setToolTip(identity.details())

    def _start_device_watch(self, target):
        """Keep refreshing until the device shows up, then stop.

        A newly attached transport often reports 'offline' for a few seconds
        before ADB promotes it, which is why pressing Refresh by hand appeared
        to be necessary.
        """
        self._stop_device_watch()
        self._device_watch_target = target
        self._device_watch_ticks = 0
        self._device_watch_timer = QTimer(self)
        # 1s. This only asks the local ADB server for its transport list, so it
        # costs a short-lived process and never crosses the tunnel.
        self._device_watch_timer.setInterval(1000)
        self._device_watch_timer.timeout.connect(self._device_watch_tick)
        self._device_watch_timer.start()

    def _stop_device_watch(self):
        timer = getattr(self, "_device_watch_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._device_watch_timer = None

    def _device_watch_tick(self):
        if self._closing:
            self._stop_device_watch()
            return
        target = getattr(self, "_device_watch_target", "")
        # Stop as soon as the device is selectable, the operator picked one
        # themselves, or the tunnel went away.
        if self._current_device_serial() or not self.ssh_port:
            self._stop_device_watch()
            return
        self._device_watch_ticks += 1
        if self._device_watch_ticks > 40:      # about 40 seconds at 1s
            self._stop_device_watch()
            self.logline(
                "[Devices] No device appeared on the tunnel after 40s. "
                "The lease may have expired; open a fresh one on the CDM website.")
            self.status_var.set("No device on this tunnel — check the port")
            return
        self.worker(lambda: self._refresh_devices_job(preferred=target))

    # Candidate properties for the project label the CDM website shows beside
    # the serial. None is guaranteed to exist; the serial alone already answers
    # "am I on the right device", so a miss costs nothing.
    _PROJECT_PROPERTY_KEYS = (
        "persist.convrse.project",
        "persist.convrse.site",
        "persist.sys.convrse.project",
        "ro.convrse.project",
    )

    def _read_device_identity(self, serial):
        """Ask the device who it is. This is what makes a stale port harmless."""
        props = {}
        for key in ("ro.serialno", "ro.product.model", "ro.product.device",
                    "ro.build.version.release"):
            rc, out, _elapsed = legacy.adb_command(
                "shell", "getprop", key, serial=serial, timeout=15,
                telemetry=False)
            props[key] = (out or "").strip() if rc == 0 else ""
        if not any(props.values()):
            return None
        project = ""
        for key in self._PROJECT_PROPERTY_KEYS:
            rc, out, _elapsed = legacy.adb_command(
                "shell", "getprop", key, serial=serial, timeout=10,
                telemetry=False)
            value = (out or "").strip() if rc == 0 else ""
            if value:
                project = value
                break
        return conn.DeviceIdentity(
            serial=props.get("ro.serialno", ""),
            model=props.get("ro.product.model", ""),
            name=props.get("ro.product.device", ""),
            android=props.get("ro.build.version.release", ""),
            project=project,
        )

    def disconnect_tunnel(self):
        local_port = self.ssh_port
        target = f"{legacy.DEFAULT_IP}:{local_port}" if local_port else None
        proc = self.ssh_proc
        self.ssh_proc = None
        self._connected_identity = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

        def job():
            if target:
                result = legacy.adb_command("disconnect", target, timeout=20)
                self.logline(f"[ADB] disconnect {target}: {result[1] or 'done'}")
            self.ui(self._set_tunnel_state, "Disconnected", "idle")
            self.ui(self._set_route_state, "Not connected", "idle")
            self.ui(self.status_var.set, "Disconnected")
            self.ui(self.device_pill.setText, "No device")

        self.ssh_port = None
        self.ssh_remote_port = None
        self.worker(job)

    def _set_tunnel_state(self, text, tone="idle"):
        if tone == "online":
            self._external_tunnel = not (
                self.ssh_proc and self.ssh_proc.poll() is None)
        elif tone in ("idle", "error"):
            self._external_tunnel = False
        self.tunnel_label.setText(text)
        self.tunnel_label.setProperty("tone", tone)
        _repolish(self.tunnel_label)
        if tone == "online":
            self.tunnel_button_widget.setText("Disconnect")
            self.tunnel_button_widget.setProperty("role", "danger")
            self.tunnel_button_widget.setEnabled(True)
        elif tone == "pending":
            self.tunnel_button_widget.setText("Connecting…")
            self.tunnel_button_widget.setProperty("role", "primary")
            self.tunnel_button_widget.setEnabled(False)
        elif tone == "error":
            self.tunnel_button_widget.setText("Retry connection")
            self.tunnel_button_widget.setProperty("role", "warning")
            self.tunnel_button_widget.setEnabled(True)
        else:
            self.tunnel_button_widget.setText("Connect")
            self.tunnel_button_widget.setProperty("role", "primary")
            self.tunnel_button_widget.setEnabled(True)
        _repolish(self.tunnel_button_widget)

    def _device_changed(self, serial):
        serial = (serial or "").strip()
        identity = self._connected_identity
        if serial and identity:
            self._show_identity(identity)
        else:
            self.device_pill.setText(serial or "No device")
            self.device_pill.setToolTip("")

        # A live transport can have arrived two ways, and both are legitimate.
        # When this app opened the tunnel, ADB addresses the device by our
        # private local socket.  When the operator followed the CDM website's
        # own instructions in a terminal, the forward is symmetric and the
        # serial carries the leased port itself.  Recognise either.
        if serial and self.tunnel_label.property("tone") != "online":
            leased = conn.parse_endpoint(self.port_edit.text())
            if self.ssh_port and serial == f"{legacy.DEFAULT_IP}:{self.ssh_port}":
                self._set_tunnel_state(
                    f"Live on {self.ssh_remote_port or leased}", "online")
            elif leased is not None and serial == f"{legacy.DEFAULT_IP}:{leased}":
                # Someone else's terminal owns this forward; we only ride it.
                self._set_tunnel_state(f"Active on {leased}", "online")
        if not serial:
            self._guard_generation += 1
            self._stream_probe_generation += 1
            self._set_ai_pq_status("Waiting for a device", "idle")
            self._ai_guard_last_serial = None
            self._pending_restart = False
            if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
                self._mirror_requested_stop = True
                self._set_mirror_state("stopping", "Device disconnected · stopping mirror…")
                self._terminate_scrcpy()
            if self._mirror_state not in ("running", "starting", "stopping", "restarting"):
                self._set_mirror_state("stopped")
            return
        if serial != self._ai_guard_last_serial:
            self._guard_generation += 1
            generation = self._guard_generation
            self._ai_guard_last_serial = serial
            self._root_mode_by_serial.clear()
            self._root_failure_reason_by_serial.pop(serial, None)
            self._guard_status_by_serial[serial] = (
                "protecting" if self._display_protection_enabled else "disabled")
            self._load_custom_profile(serial)
            self._refresh_profile_combo_labels()
            if self._display_protection_enabled:
                self._set_ai_pq_status("Applying display protection…", "pending")
            else:
                self._set_ai_pq_status(
                    "Off · mirror starts without display protection", "idle")
            if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
                self._mirror_requested_stop = True
                self._pending_restart = False
                self._terminate_scrcpy()
            self._set_mirror_state(
                "ready",
                "Connected · display protection running in background…"
                if self._display_protection_enabled
                else "Connected · display protection off",
            )
            self._start_stream_capability_probe(serial)
            if not self._display_protection_enabled:
                self._guard_status_by_serial[serial] = "disabled"
                self._set_ai_pq_status(
                    "Off · mirror starts without display protection", "idle")
                return
            self.enforce_ai_pq_off(
                silent=True,
                on_complete=lambda success, target=serial, token=generation:
                    self._finish_initial_guard(target, token, success),
                apply_all=AUTO_ENFORCE_ON_CONNECT,
            )
            return

        guard_status = self._guard_status_by_serial.get(serial)
        if guard_status in ("protected", "not_applicable", "disabled"):
            if self._mirror_state in ("stopped", "failed"):
                detail = (
                    "Display protection off · ready to mirror"
                    if guard_status == "disabled"
                    else None
                )
                self._set_mirror_state("ready", detail)
        elif guard_status == "failed":
            if self._mirror_state in ("stopped", "failed"):
                self._set_mirror_state(
                    "ready", "Ready · turn display processing off manually")

    def _finish_initial_guard(self, serial, generation, success):
        if self._closing or generation != self._guard_generation:
            return
        if serial != self._current_device_serial():
            return
        outcome = self._guard_status_by_serial.get(serial)
        if success and outcome in ("protected", "not_applicable"):
            self.ai_pq_status_label.setToolTip("")
            if self._mirror_state in ("stopped", "ready", "failed"):
                self._set_mirror_state("ready", "Display protected · ready to mirror")
            return
        reason = self._guard_failure_reason_by_serial.get(
            serial, "Display protection could not be verified")
        self._set_ai_pq_status(
            "Display protection failed · turn display processing off manually",
            "error",
        )
        self.ai_pq_status_label.setToolTip(reason)
        self.logline(
            f"[AI-PQ] {serial}: display protection failed ({reason}). "
            "Mirror remains available; switch display processing off manually."
        )
        if self._mirror_state in ("stopped", "ready", "failed"):
            self._set_mirror_state(
                "ready", "Ready · turn display processing off manually")
        elif self._mirror_state == "running":
            self.start_button_widget.setToolTip(
                "Mirror is live · turn display processing off manually")

    def _preset_combo_changed(self, index):
        name = self.profile_combo.itemData(int(index))
        if not name:
            return
        name = str(name)
        if name == CUSTOM_PRESET:
            self._load_custom_profile()
            self.custom_profile_panel.show()
            if self.selected_preset != CUSTOM_PRESET:
                self.mirror_status_label.setText(
                    "Choose Custom settings, then press Apply custom")
            return
        self.custom_profile_panel.hide()
        self.select_preset(name)

    def select_preset(self, name, restart=True):
        if name not in PROFILE_NAMES:
            return
        if name == CUSTOM_PRESET:
            custom_index = self.profile_combo.findData(CUSTOM_PRESET)
            if custom_index >= 0:
                self.profile_combo.setCurrentIndex(custom_index)
            self.custom_profile_panel.show()
            return
        changed = name != self.selected_preset
        self.selected_preset = name
        self.custom_profile_panel.hide()
        selected_index = self.profile_combo.findData(name)
        if selected_index >= 0 and selected_index != self.profile_combo.currentIndex():
            was_blocked = self.profile_combo.blockSignals(True)
            self.profile_combo.setCurrentIndex(selected_index)
            self.profile_combo.blockSignals(was_blocked)
        self.settings.setValue("stream/preset", name)
        self._update_stream_metrics_label()
        if changed and restart and self._mirror_state in ("running", "starting"):
            self._pending_restart = True
            self._set_mirror_state("restarting", f"Restarting in {name}…")
            self._terminate_scrcpy()
        elif changed:
            self.mirror_status_label.setText(f"{name} selected · ready to start")

    def _target_metric_text(self):
        preset = self._resolve_stream_config()
        return (
            f"Target {preset.resolution} · {self._bitrate_label(preset.bitrate_bps)} · "
            f"≤{preset.max_fps} FPS · H.264")

    def _update_stream_metrics_label(self):
        preset = self._resolve_stream_config()
        actual = []
        if self._scrcpy_resolution:
            actual.append(self._scrcpy_resolution)
        if self._scrcpy_fps is not None:
            actual.append(f"{self._scrcpy_fps:.1f} FPS")
        if actual:
            target = []
            if not self._scrcpy_resolution:
                target.append(preset.resolution)
            if self._scrcpy_fps is None:
                target.append(f"≤{preset.max_fps} FPS")
            text = "Actual " + " · ".join(actual)
            if target:
                text = "Target " + " · ".join(target) + "  |  " + text
            self.stream_metrics_label.setText(text)
            return
        self.stream_metrics_label.setText(self._target_metric_text())

    def _resolve_stream_config(self, serial=None):
        serial = (serial or self._current_device_serial() or "").strip()
        preset = (
            self._custom_preset()
            if self.selected_preset == CUSTOM_PRESET
            else STREAM_PRESETS.get(self.selected_preset, STREAM_PRESETS[DEFAULT_PRESET])
        )
        capabilities = self._stream_capabilities_for(serial)
        max_fps = preset.max_fps
        if max_fps is None:
            refresh = capabilities.refresh_hz or 60.0
            max_fps = max(15, min(60, int(round(refresh))))
        resolution = self._limited_resolution_label(
            preset.max_size, capabilities, preset.resolution)
        encoder = (
            None if serial in self._stream_encoder_bypass
            else capabilities.h264_encoder
        )
        return ResolvedStreamConfig(
            name=self.selected_preset,
            resolution=resolution,
            max_size=preset.max_size,
            bitrate_bps=preset.bitrate_bps,
            max_fps=max_fps,
            h264_encoder=encoder,
        )

    @staticmethod
    def _parse_wm_size(output):
        matches = re.findall(
            r"(?:Physical|Override) size:\s*(\d+)x(\d+)", output or "")
        if not matches:
            matches = re.findall(r"(?<!\d)(\d{3,5})x(\d{3,5})(?!\d)", output or "")
        if not matches:
            return None, None
        width, height = map(int, matches[-1])
        return (width, height) if width > 0 and height > 0 else (None, None)

    @staticmethod
    def _parse_refresh_rate(output):
        patterns = (
            r"mActiveSfDisplayMode=.*?(?:fps|refreshRate)=([\d.]+)",
            r"mActiveModeId=.*?renderFrameRate=([\d.]+)",
            r"refreshRate=([\d.]+)",
            r"\bfps=([\d.]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, output or "", re.IGNORECASE)
            if match:
                value = float(match.group(1))
                if 1.0 <= value <= 240.0:
                    return value
        return None

    @staticmethod
    def _parse_h264_hardware_encoder(output):
        for line in (output or "").splitlines():
            normalized = line.strip()
            if "--video-codec=h264" not in normalized or "(hw)" not in normalized:
                continue
            if "(alias)" in normalized.lower():
                continue
            match = re.search(
                r"--video-encoder=(?:['\"])?([^'\"\s]+)", normalized)
            if match:
                return match.group(1)
        return None

    def _probe_stream_capabilities(self, serial):
        errors = []
        size_result = legacy.adb_command(
            "shell", "wm", "size", serial=serial, timeout=20, telemetry=False)
        width, height = self._parse_wm_size(size_result[1] if size_result[0] == 0 else "")
        if not width:
            errors.append("display size unavailable")

        display_result = legacy.adb_command(
            "shell", "dumpsys", "display",
            serial=serial, timeout=30, telemetry=False)
        refresh_hz = self._parse_refresh_rate(
            display_result[1] if display_result[0] == 0 else "")
        if refresh_hz is None:
            errors.append("refresh rate unavailable")

        sdk_result = legacy.adb_command(
            "shell", "getprop", "ro.build.version.sdk",
            serial=serial, timeout=15, telemetry=False)
        sdk_text = (sdk_result[1] if sdk_result[0] == 0 else "").strip()
        sdk = int(sdk_text) if sdk_text.isdigit() else None

        encoder = None
        try:
            encoder_result = subprocess.run(
                [legacy.SCRCPY, "-s", serial, "--list-encoders"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=50,
                creationflags=legacy._NO_WINDOW,
            )
            encoder = self._parse_h264_hardware_encoder(encoder_result.stdout)
            if encoder_result.returncode != 0 or not encoder:
                errors.append("hardware H.264 probe unavailable")
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"encoder probe failed: {exc}")

        return DeviceStreamCapabilities(
            width=width,
            height=height,
            refresh_hz=refresh_hz,
            sdk=sdk,
            h264_encoder=encoder,
            probe_error="; ".join(errors) or None,
        )

    def _start_stream_capability_probe(self, serial):
        serial = (serial or "").strip()
        if not serial:
            return
        cached = self._stream_capabilities.get(serial)
        if cached is not None:
            self._apply_stream_capabilities(serial, self._stream_probe_generation, cached)
            return
        self._stream_probe_generation += 1
        generation = self._stream_probe_generation
        self._stream_probe_serial = serial

        def job():
            capabilities = self._probe_stream_capabilities(serial)
            self.ui(
                self._apply_stream_capabilities,
                serial, generation, capabilities)

        self.worker(job)

    def _apply_stream_capabilities(self, serial, generation, capabilities):
        if generation != self._stream_probe_generation:
            return
        if serial != self._current_device_serial():
            return
        self._stream_capabilities[serial] = capabilities
        self._refresh_profile_combo_labels()
        self._update_stream_metrics_label()
        if capabilities.probe_error:
            self.logline(
                f"[Stream] {serial}: {capabilities.probe_error}; safe fallbacks enabled.")
        else:
            self.logline(
                f"[Stream] {serial}: {capabilities.width}x{capabilities.height} "
                f"@ {capabilities.refresh_hz:g} Hz · {capabilities.h264_encoder}.")
        if self._mirror_state in ("stopped", "ready", "failed"):
            guard_status = self._guard_status_by_serial.get(serial)
            detail = (
                "Ready · turn display processing off manually"
                if guard_status == "failed"
                else (
                    "Device ready · display protection off"
                    if guard_status == "disabled"
                    else "Device ready to mirror"
                )
            )
            self._set_mirror_state("ready", detail)

    @staticmethod
    def _build_scrcpy_command(serial, title, config):
        command = [
            legacy.SCRCPY, "-s", serial,
            "--video-codec", "h264",
        ]
        if config.h264_encoder:
            command.extend(["--video-encoder", config.h264_encoder])
        if config.max_size > 0:
            command.extend(["--max-size", str(config.max_size)])
        command.extend([
            "--video-bit-rate", str(config.bitrate_bps),
            "--max-fps", str(config.max_fps),
            "--audio-source", AUDIO_SOURCE,
            "--audio-codec", AUDIO_CODEC,
            "--audio-bit-rate", AUDIO_BITRATE,
            "--audio-buffer", str(AUDIO_BUFFER_MS),
            "--print-fps",
            "--window-title", title,
            "--window-borderless",
        ])
        # Ask for a position past the far corner of the whole desktop so the
        # window has the best chance of never being painted where the operator
        # can see it.  SDL may clamp this onto a real display, which is why the
        # window is also hidden the moment it is found and only shown again once
        # it lives inside the mirror frame.
        offset_x, offset_y = _offscreen_origin()
        command.extend([
            "--window-x", str(offset_x),
            "--window-y", str(offset_y),
        ])
        return command

    # ---------- stateful mirror lifecycle ----------
    def _toggle_mirror(self):
        if self._mirror_state == "running":
            self.stop_scrcpy()
        elif self._mirror_state in ("ready", "stopped", "failed"):
            self.start_scrcpy()

    def _set_mirror_state(self, state, detail=None):
        was_running = self._mirror_state == "running"
        self._mirror_state = state
        if (was_running and state in ("stopped", "ready", "failed")
                and self._health_recheck_pending):
            # The operator opened the mirror from the morning check, fixed
            # whatever was wrong, and closed it. Re-check rather than making
            # them remember to press the button again.
            self._health_recheck_pending = False
            QTimer.singleShot(1200, self.run_health_check)
        serial_ready = bool(self.active_serial())
        table = {
            "stopped": (
                "Start mirror", "primary" if serial_ready else "quiet",
                serial_ready, "Stopped", "idle"),
            "ready": (
                "Start mirror", "primary", serial_ready,
                f"{self.selected_preset} · ready", "idle"),
            "starting": ("Starting…", "primary", False, "Starting mirror…", "pending"),
            "running": ("Stop mirror", "danger", True, "Live", "online"),
            "stopping": ("Stopping…", "danger", False, "Stopping mirror…", "pending"),
            "restarting": ("Restarting…", "warning", False, "Restarting mirror…", "pending"),
            "failed": ("Restart mirror", "warning", serial_ready, "Mirror stopped unexpectedly", "error"),
        }
        text, role, enabled, default_detail, tone = table[state]
        self.start_button_widget.setText(text)
        self.start_button_widget.setProperty("role", role)
        self.start_button_widget.setEnabled(enabled)
        self.start_button_widget.setToolTip(
            "Connect a device to start mirroring"
            if not serial_ready and state in ("stopped", "ready", "failed")
            else detail or default_detail)
        _repolish(self.start_button_widget)
        self.mirror_status_label.setText(detail or default_detail)
        self.mirror_status_label.setProperty("tone", tone)
        _repolish(self.mirror_status_label)

    def start_scrcpy(self):
        serial = self.require_serial()
        if not serial:
            self._set_mirror_state("stopped")
            return
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            self._set_mirror_state("running", "Mirror is already running")
            return
        config = self._resolve_stream_config(serial)
        title = f"{legacy.APP_SHORT_NAME} Mirror {time.time_ns()}"
        command = self._build_scrcpy_command(serial, title, config)
        self._mirror_requested_stop = False
        self._pending_restart = False
        self._scrcpy_fps = None
        self._scrcpy_resolution = None
        self._scrcpy_output_lines = []
        self._current_stream_config = config
        self._mirror_generation += 1
        generation = self._mirror_generation
        self._set_mirror_state(
            "starting", f"Starting {self.selected_preset} · {config.resolution}…")
        self._update_stream_metrics_label()
        try:
            self.scrcpy_proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                creationflags=legacy._NO_WINDOW,
            )
            self.logline("[Mirror] " + legacy.command_text(command))
            proc = self.scrcpy_proc
            self.worker(lambda: self._read_scrcpy_output(proc, generation))
            self.worker(lambda: self._watch_scrcpy_window(proc, title, generation))
            self.after(500, self._poll_scrcpy, proc, generation)
        except FileNotFoundError:
            self.scrcpy_proc = None
            self._set_mirror_state("failed", "scrcpy was not found")
            v1.QtMessageBoxAdapter.showerror(
                APP_NAME, "scrcpy was not found. Place scrcpy.exe beside this app or on PATH.")
        except Exception as exc:
            self.scrcpy_proc = None
            self._set_mirror_state("failed", str(exc))
            v1.QtMessageBoxAdapter.showerror(APP_NAME, f"Could not start the mirror:\n{exc}")

    def _is_current_mirror(self, proc, generation):
        return self.scrcpy_proc is proc and self._mirror_generation == generation

    def _read_scrcpy_output(self, proc, generation):
        try:
            if proc.stdout is None:
                return
            for raw_line in iter(proc.stdout.readline, ""):
                line = raw_line.strip()
                if not line:
                    continue
                if self._is_current_mirror(proc, generation):
                    self._scrcpy_output_lines.append(line)
                    del self._scrcpy_output_lines[:-80]
                fps_match = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*fps\b", line, re.IGNORECASE)
                fps = float(fps_match.group(1)) if fps_match else None
                size_match = re.search(
                    r"(?:Texture|video|frame|size)[^\d]*(\d{3,5})x(\d{3,5})",
                    line, re.IGNORECASE)
                width = height = None
                if size_match:
                    width, height = int(size_match.group(1)), int(size_match.group(2))
                if (fps is not None or width is not None) and \
                        self._is_current_mirror(proc, generation):
                    self.ui(
                        self._apply_stream_metrics,
                        proc, generation, fps, width, height)
                if ("ERROR" in line.upper() or "WARN" in line.upper()) and \
                        self._is_current_mirror(proc, generation):
                    self.logline("[scrcpy] " + line)
        except Exception as exc:
            self.logline(f"[scrcpy output] {exc}")
        finally:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

    def _poll_scrcpy(self, proc, generation):
        if not self._is_current_mirror(proc, generation):
            return
        if proc.poll() is None:
            self._metrics_tick += 1
            if (self._mirror_state in ("starting", "running")
                    and self._metrics_tick % 4 == 0
                    and self._scrcpy_resolution is None):
                self.worker(
                    lambda: self._refresh_mirror_source_size(proc, generation))
            self.after(500, self._poll_scrcpy, proc, generation)
            return
        code = proc.returncode
        self.scrcpy_proc = None
        self._clear_mirror_embed()
        if self._pending_restart:
            self._pending_restart = False
            if self.active_serial():
                self.after(250, self.start_scrcpy)
            else:
                self._set_mirror_state("stopped", "Device disconnected")
        elif self._mirror_requested_stop or code == 0:
            self._set_mirror_state("ready" if self.active_serial() else "stopped", "Mirror stopped")
        else:
            config = self._current_stream_config
            output = "\n".join(self._scrcpy_output_lines).casefold()
            encoder_failure = any(
                marker in output
                for marker in (
                    "video encoder", "failed to create encoder", "mediacodec",
                    "codec error", "encoder error", "could not configure codec",
                )
            )
            serial = self.active_serial()
            if (
                    config is not None
                    and config.h264_encoder
                    and serial
                    and serial not in self._stream_encoder_bypass
                    and encoder_failure):
                self._stream_encoder_bypass.add(serial)
                self.logline(
                    f"[Stream] {config.h264_encoder} failed; retrying once with "
                    "scrcpy automatic encoder selection.")
                self._set_mirror_state(
                    "restarting", "Hardware encoder failed · using safe fallback…")
                self.after(250, self.start_scrcpy)
                return
            self._set_mirror_state("failed", f"Mirror exited with code {code}")

    def _terminate_scrcpy(self):
        proc = self.scrcpy_proc
        if proc and proc.poll() is None:
            # Invalidate the output/HWND workers immediately, then give a new
            # generation-matched poller ownership of final cleanup.
            self._mirror_generation += 1
            stop_generation = self._mirror_generation
            self.after(100, self._poll_scrcpy, proc, stop_generation)
            try:
                proc.terminate()
            except Exception:
                pass

            def kill_if_needed():
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            self.after(1800, kill_if_needed)

    def stop_scrcpy(self):
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            restart_requested = self._pending_restart
            self._mirror_requested_stop = not restart_requested
            if restart_requested:
                self._set_mirror_state("restarting", f"Restarting in {self.selected_preset}…")
            else:
                self._pending_restart = False
                self._set_mirror_state("stopping")
            self._terminate_scrcpy()
        else:
            self._clear_mirror_embed()
            self._set_mirror_state("ready" if self.active_serial() else "stopped")

    def _watch_scrcpy_window(self, proc, title, generation):
        """Find only the current scrcpy window and ignore stale launch workers."""
        if legacy._USER32 is None:
            return
        hwnd = 0
        for _ in range(600):
            if proc.poll() is not None or not self._is_current_mirror(proc, generation):
                return
            if self._mirror_state in ("stopping", "restarting"):
                return
            hwnd = self._find_scrcpy_hwnd(proc, title)
            if hwnd:
                # scrcpy is launched off the desktop, but SDL clamps a window
                # onto the nearest display, so it becomes briefly visible as a
                # separate window before Qt re-parents it.  Hide it the instant
                # it exists; adopt_window shows it again inside the mirror frame.
                try:
                    legacy._USER32.ShowWindow(int(hwnd), 0)  # SW_HIDE
                except Exception:
                    pass
                break
            time.sleep(0.1)
        if not hwnd:
            if self._is_current_mirror(proc, generation) and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
                self.set_status("Mirror window could not be embedded — start it again")
                self.logline(
                    "[Mirror] scrcpy window was not found; the hidden process was closed.")
            return
        self.ui(self._adopt_scrcpy_window_for_process, proc, hwnd, generation)

    def _adopt_scrcpy_window(self, hwnd):
        """Compatibility entry point for inherited helpers."""
        proc = self.scrcpy_proc
        if proc is not None:
            self._adopt_scrcpy_window_for_process(
                proc, hwnd, self._mirror_generation)

    def _adopt_scrcpy_window_for_process(self, proc, hwnd, generation):
        if (not self._is_current_mirror(proc, generation)
                or proc.poll() is not None
                or self._mirror_state in ("stopping", "restarting")):
            return
        try:
            self.scrcpy_hwnd = hwnd
            if self.mirror_aspect:
                self.mirror_frame.set_aspect(self.mirror_aspect)
            self.mirror_frame.adopt_window(hwnd)
            self._set_mirror_state("running", f"{self.selected_preset} stream is live")
            self.logline("[Mirror] scrcpy embedded with aspect-fit geometry.")
            self.worker(
                lambda: self._refresh_mirror_source_size(proc, generation))
        except Exception as exc:
            self.scrcpy_hwnd = None
            self._set_mirror_state("failed", f"Embedding failed: {exc}")
            self.logline(f"[Mirror] Could not embed scrcpy ({exc}).")
            if self._is_current_mirror(proc, generation):
                self._mirror_requested_stop = False
                self._terminate_scrcpy()

    def _apply_stream_metrics(self, proc, generation, fps=None, width=None, height=None):
        if not self._is_current_mirror(proc, generation):
            return
        if fps is not None:
            self._scrcpy_fps = float(fps)
        if width and height:
            self._scrcpy_resolution = f"{int(width)} × {int(height)}"
            self.mirror_aspect = int(width) / int(height)
            self.mirror_frame.set_aspect(self.mirror_aspect)
        self._update_stream_metrics_label()

    def _clear_mirror_embed(self):
        self.scrcpy_hwnd = None
        self.mirror_aspect = None
        self.mirror_frame.clear_window()
        self._scrcpy_fps = None
        self._scrcpy_resolution = None
        self._update_stream_metrics_label()

    def _resize_embedded_mirror(self):
        self.mirror_frame._layout_content()

    def _refit_mirror(self):
        self.mirror_frame._layout_content()
        self.status_var.set("Mirror fitted without cropping")

    def _refresh_mirror_source_size(self, proc=None, generation=None):
        if proc is not None and not self._is_current_mirror(proc, generation):
            return
        if self._scrcpy_resolution is not None:
            return
        serial = self.active_serial()
        if not serial:
            return
        result = legacy.adb_command(
            "shell", "wm", "size", serial=serial, timeout=15, telemetry=False)
        if result[0] != 0:
            return
        matches = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", result[1])
        if not matches:
            matches = re.findall(r"(\d+)x(\d+)", result[1])
        if not matches:
            return
        width, height = map(int, matches[-1])
        if width <= 0 or height <= 0:
            return
        if proc is not None and not self._is_current_mirror(proc, generation):
            return
        self.mirror_aspect = width / height
        self.ui(self.mirror_frame.set_aspect, self.mirror_aspect)

    # ---------- focus/full-screen mode ----------
    def toggle_focus_mode(self):
        self.set_focus_mode(not self._focus_mode)

    def set_focus_mode(self, enabled):
        enabled = bool(enabled)
        if enabled == self._focus_mode:
            return
        if enabled:
            self._focus_restore = {
                "maximized": self.isMaximized(),
                "geometry": self.saveGeometry(),
                "menu": self.menuBar().isVisible(),
                "sidebar": self.sidebar.isVisible(),
                "console": not self._console_collapsed,
                "main": self.main_splitter.saveState(),
                "workspace": self.workspace_splitter.saveState(),
            }
            self._focus_mode = True
            self.menuBar().hide()
            self.app_bar.hide()
            self.sidebar.hide()
            self.console.hide()
            self.showFullScreen()
            self._sync_focus_action()
        else:
            self.exit_focus_mode()

    def exit_focus_mode(self):
        if not self._focus_mode:
            return
        restore = dict(self._focus_restore)
        self._focus_mode = False
        self.showNormal()
        geometry = restore.get("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        if restore.get("maximized", True):
            self.showMaximized()
        self.menuBar().setVisible(
            self._bool_value(self.settings.value("ui/menu_bar_visible", True), True))
        self.app_bar.show()
        self.console.show()
        self._set_sidebar_visible(restore.get("sidebar", True), persist=False)
        self._set_console_visible(restore.get("console", True), persist=False)
        self._sync_focus_action()

        def restore_splitters():
            if restore.get("main"):
                self.main_splitter.restoreState(restore["main"])
            if restore.get("workspace") and not self._console_collapsed:
                self.workspace_splitter.restoreState(restore["workspace"])

        QTimer.singleShot(0, restore_splitters)

    def _sync_focus_action(self):
        action = getattr(self, "focus_action", None)
        if action is not None and action.isChecked() != self._focus_mode:
            action.blockSignals(True)
            action.setChecked(self._focus_mode)
            action.blockSignals(False)
        button = getattr(self, "header_focus_button", None)
        if button is not None:
            button.setText("Exit full screen" if self._focus_mode else "Full screen")

    def _escape_focus(self):
        if self._focus_mode:
            self.exit_focus_mode()

    # ---------- authoritative RK3576 display guard ----------
    def _set_ai_pq_status(self, text, tone="idle"):
        self.ai_pq_status_label.setText(text)
        self.ai_pq_status_label.setProperty("tone", tone)
        _repolish(self.ai_pq_status_label)

    def _render_ai_evaluation(
            self, evaluation, supported_properties=None, preference_booleans=None):
        supported = (
            set(supported_properties) if supported_properties is not None else None)
        for item in evaluation.toggles:
            label = self.ai_toggle_labels.get(item.name)
            if label is None:
                continue
            cached_on = (
                preference_booleans is not None
                and preference_booleans.get(item.preference_key) is True)
            if cached_on:
                text, tone = "ON · cached", "error"
            elif supported is not None and item.property_key not in supported:
                text, tone = "N/A", "idle"
            elif item.state is ToggleState.OFF:
                text, tone = "OFF", "online"
            elif item.state is ToggleState.ON:
                text, tone = "ON", "error"
            elif item.state is ToggleState.MISSING:
                text, tone = "MISSING", "error"
            else:
                text, tone = str(item.raw_value or "UNKNOWN"), "error"
            label.setText(text)
            label.setProperty("tone", tone)
            _repolish(label)

    def _root_access_mode(self, serial):
        cached = self._root_mode_by_serial.get(serial)
        if cached:
            cached_identity = legacy.adb_command(
                *self._root_command_args(cached, "id"),
                serial=serial, timeout=15, telemetry=False)
            if cached_identity[0] == 0 and "uid=0(root)" in cached_identity[1]:
                return cached
            self._root_mode_by_serial.pop(serial, None)

        diagnostics = []
        identity = legacy.adb_command(
            "shell", "id", serial=serial, timeout=15, telemetry=False)
        if identity[0] == 0 and "uid=0(root)" in identity[1]:
            self._root_mode_by_serial[serial] = "adb"
            self._root_failure_reason_by_serial.pop(serial, None)
            return "adb"
        diagnostics.append(self._adb_result_output(identity) or "adbd is not root")
        su_identity = legacy.adb_command(
            "shell", "su", "0", "id",
            serial=serial, timeout=15, telemetry=False)
        if su_identity[0] == 0 and "uid=0(root)" in su_identity[1]:
            self._root_mode_by_serial[serial] = "su"
            self._root_failure_reason_by_serial.pop(serial, None)
            return "su"
        diagnostics.append(self._adb_result_output(su_identity) or "su root was denied")

        root_result = legacy.adb_command(
            "root", serial=serial, timeout=35, telemetry=False)
        root_output = self._adb_result_output(root_result)
        diagnostics.append(root_output or f"adb root exit code {root_result[0]}")
        if root_result[0] == 0 and "cannot run as root" not in root_output.casefold():
            time.sleep(2.0)
            legacy.run_command([legacy.ADB, "connect", serial], timeout=20)
            rooted_identity = legacy.adb_command(
                "shell", "id", serial=serial, timeout=20, telemetry=False)
            if rooted_identity[0] == 0 and "uid=0(root)" in rooted_identity[1]:
                self._root_mode_by_serial[serial] = "adb"
                self._root_failure_reason_by_serial.pop(serial, None)
                return "adb"
            diagnostics.append(
                self._adb_result_output(rooted_identity)
                or "device did not reconnect as root")

        combined = " · ".join(
            dict.fromkeys(" ".join(item.split()) for item in diagnostics if item))
        lowered = combined.casefold()
        if any(token in lowered for token in (
                "offline", "no devices", "device not found", "closed", "timeout")):
            reason = "Device offline or SSH/ADB tunnel unavailable"
        elif "cannot run as root" in lowered or "permission denied" in lowered:
            reason = "Device is not rooted or root permission was denied"
        else:
            reason = "Root identity could not be verified"
        if combined:
            reason += f" · {combined[:180]}"
        self._root_failure_reason_by_serial[serial] = reason
        return None

    @staticmethod
    def _root_setprop_args(mode, name, value):
        if mode == "su":
            return "shell", "su", "0", "setprop", name, value
        return "shell", "setprop", name, value

    @staticmethod
    def _root_command_args(mode, *args):
        if mode == "su":
            return "shell", "su", "0", *args
        if mode == "adb":
            return "shell", *args
        raise ValueError(f"Unsupported root mode: {mode!r}")

    @staticmethod
    def _adb_result_output(result):
        return str(result[1] if len(result) > 1 else "").strip()

    def _checked_adb(
            self, *args, serial, operation, timeout=20, telemetry=False):
        result = legacy.adb_command(
            *args, serial=serial, timeout=timeout, telemetry=telemetry)
        if result[0] != 0:
            detail = self._adb_result_output(result) or f"exit code {result[0]}"
            raise RuntimeError(f"{operation}: {detail}")
        return self._adb_result_output(result)

    def _checked_root_adb(
            self, serial, root_mode, *args, operation, timeout=20):
        return self._checked_adb(
            *self._root_command_args(root_mode, *args),
            serial=serial,
            operation=operation,
            timeout=timeout,
        )

    def _root_path_exists(self, serial, root_mode, path):
        result = legacy.adb_command(
            *self._root_command_args(root_mode, "test", "-e", path),
            serial=serial, timeout=15, telemetry=False)
        output = self._adb_result_output(result)
        if result[0] == 0:
            return True
        if result[0] == 1 and not output:
            return False
        raise RuntimeError(
            "Could not inspect rooted device path: "
            + (output or f"exit code {result[0]}"))

    def _current_android_user(self, serial):
        result = legacy.adb_command(
            "shell", "am", "get-current-user",
            serial=serial, timeout=15, telemetry=False)
        output = self._adb_result_output(result)
        match = re.search(r"\b(\d+)\b", output)
        if result[0] == 0 and match:
            return int(match.group(1))
        fallback = legacy.adb_command(
            "shell", "cmd", "activity", "get-current-user",
            serial=serial, timeout=15, telemetry=False)
        fallback_output = self._adb_result_output(fallback)
        match = re.search(r"\b(\d+)\b", fallback_output)
        if fallback[0] == 0 and match:
            return int(match.group(1))
        detail = fallback_output or output or "no response"
        raise RuntimeError(f"Could not determine Android user: {detail}")

    def _resolve_ai_preferences_path(self, serial, root_mode):
        cached = self._ai_preferences_path_by_serial.get(serial)
        if cached and self._root_path_exists(serial, root_mode, cached):
            return cached
        user_id = self._current_android_user(serial)
        filename = f"{AI_SETTINGS_PACKAGE}_preferences.xml"
        candidates = (
            f"/data/user_de/{user_id}/{AI_SETTINGS_PACKAGE}/shared_prefs/{filename}",
            f"/data/user/{user_id}/{AI_SETTINGS_PACKAGE}/shared_prefs/{filename}",
            f"/data/data/{AI_SETTINGS_PACKAGE}/shared_prefs/{filename}"
            if user_id == 0 else "",
        )
        for candidate in candidates:
            if candidate and self._root_path_exists(serial, root_mode, candidate):
                self._ai_preferences_path_by_serial[serial] = candidate
                return candidate
        raise RuntimeError(
            f"Settings preferences were not found for Android user {user_id}")

    def _adb_binary_command(self, serial, *args, timeout=20):
        command = [legacy.ADB, "-s", serial, *map(str, args)]
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                creationflags=getattr(legacy, "_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise RuntimeError("ADB executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ADB binary read timed out") from exc
        except OSError as exc:
            raise RuntimeError(f"ADB binary read failed: {exc}") from exc
        return process.returncode, process.stdout, process.stderr

    def _read_root_file_bytes(self, serial, root_mode, path):
        root_prefix = ("su", "0") if root_mode == "su" else ()
        result = self._adb_binary_command(
            serial, "exec-out", *root_prefix, "cat", path, timeout=20)
        if result[0] != 0:
            detail = result[2].decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                "Could not read Settings preferences: "
                + (detail or f"exit code {result[0]}"))
        return result[1]

    def _settings_process_ids(self, serial):
        result = legacy.adb_command(
            "shell", "pidof", AI_SETTINGS_PACKAGE,
            serial=serial, timeout=15, telemetry=False)
        output = self._adb_result_output(result)
        if result[0] == 1 and not output:
            return ()
        if result[0] != 0:
            raise RuntimeError(
                "Could not inspect the Settings process: "
                + (output or f"exit code {result[0]}"))
        process_ids = tuple(output.split())
        if not process_ids or any(not item.isdigit() for item in process_ids):
            raise RuntimeError("Settings returned an invalid process identifier")
        return process_ids

    def _settings_is_foreground(self, serial):
        package, diagnostics = self.detect_foreground_package(serial)
        if not package:
            detail = (diagnostics or "").strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise RuntimeError("Could not determine the foreground package" + suffix)
        return package == AI_SETTINGS_PACKAGE

    def _force_stop_settings(self, serial):
        self._checked_adb(
            "shell", "am", "force-stop", AI_SETTINGS_PACKAGE,
            serial=serial,
            operation="Could not stop Settings before cache synchronization",
        )
        for attempt in range(6):
            if not self._settings_process_ids(serial):
                return
            if attempt < 5:
                time.sleep(0.25)
        raise RuntimeError("Settings remained running after force-stop")

    def _restore_remote_context(
            self, serial, root_mode, target_path, reference_path):
        restore_result = legacy.adb_command(
            *self._root_command_args(root_mode, "restorecon", target_path),
            serial=serial, timeout=20, telemetry=False)
        if restore_result[0] == 0:
            return "restorecon"
        chcon_result = legacy.adb_command(
            *self._root_command_args(
                root_mode, "chcon", f"--reference={reference_path}", target_path),
            serial=serial, timeout=20, telemetry=False)
        if chcon_result[0] == 0:
            return "chcon"
        details = (
            self._adb_result_output(chcon_result)
            or self._adb_result_output(restore_result)
            or "restorecon and chcon failed"
        )
        raise RuntimeError(f"Could not restore Settings file context: {details}")

    def _write_root_preferences_atomically(
            self, serial, root_mode, xml_data, preferences_path=None):
        preferences_path = preferences_path or AI_SETTINGS_PREFS_PATH
        preferences_dir = preferences_path.rsplit("/", 1)[0]
        payload = xml_data.encode("utf-8") if isinstance(xml_data, str) else xml_data
        token = uuid.uuid4().hex
        remote_stage = f"/data/local/tmp/cdc-ai-prefs-{token}.xml"
        remote_temp = (
            f"{preferences_dir}/.cdc-ai-prefs-{token}.tmp"
        )
        local_path = None
        replaced = False
        try:
            with tempfile.NamedTemporaryFile(
                    mode="wb", prefix="cdc-ai-prefs-", suffix=".xml",
                    delete=False) as handle:
                local_path = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            self._checked_adb(
                "push", local_path, remote_stage,
                serial=serial,
                operation="Could not stage Settings preferences",
                timeout=30,
            )
            self._checked_root_adb(
                serial, root_mode, "cp", remote_stage, remote_temp,
                operation="Could not copy staged Settings preferences",
            )
            self._checked_root_adb(
                serial, root_mode, "chown", "system:system", remote_temp,
                operation="Could not restore Settings preference ownership",
            )
            self._checked_root_adb(
                serial, root_mode, "chmod", "0660", remote_temp,
                operation="Could not restore Settings preference permissions",
            )
            self._restore_remote_context(
                serial, root_mode, remote_temp, preferences_path)
            self._checked_root_adb(
                serial, root_mode, "mv", "-f", remote_temp,
                preferences_path,
                operation="Could not atomically replace Settings preferences",
            )
            replaced = True

            # The atomic move preserves the context applied to the temporary
            # inode. Ask Android to normalize it once more when restorecon is
            # available; failure is safe because the pre-move context was
            # already verified through restorecon/chcon.
            legacy.adb_command(
                *self._root_command_args(
                    root_mode, "restorecon", preferences_path),
                serial=serial, timeout=20, telemetry=False)
            self._checked_root_adb(
                serial, root_mode, "sync",
                operation="Could not flush Settings preferences",
                timeout=30,
            )
        finally:
            if local_path:
                try:
                    os.unlink(local_path)
                except FileNotFoundError:
                    pass
            legacy.adb_command(
                "shell", "rm", "-f", remote_stage,
                serial=serial, timeout=15, telemetry=False)
            if not replaced:
                legacy.adb_command(
                    *self._root_command_args(
                        root_mode, "rm", "-f", remote_temp),
                    serial=serial, timeout=15, telemetry=False)

    @staticmethod
    def _ui_text_center(xml_data, expected_text):
        try:
            root = ElementTree.fromstring(xml_data)
        except (ElementTree.ParseError, TypeError) as exc:
            raise RuntimeError("Could not parse the Settings UI hierarchy") from exc
        expected = " ".join(expected_text.split()).casefold()
        for node in root.iter():
            labels = (node.get("text", ""), node.get("content-desc", ""))
            if not any(" ".join(label.split()).casefold() == expected for label in labels):
                continue
            bounds = re.fullmatch(
                r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                node.get("bounds", ""),
            )
            if not bounds:
                continue
            left, top, right, bottom = map(int, bounds.groups())
            if right > left and bottom > top:
                return (left + right) // 2, (top + bottom) // 2
        raise RuntimeError(f"Settings item not found: {expected_text}")

    def _tap_settings_text(self, serial, expected_text):
        remote_dump = f"/data/local/tmp/cdc-ai-nav-{uuid.uuid4().hex}.xml"
        try:
            self._checked_adb(
                "shell", "uiautomator", "dump", remote_dump,
                serial=serial,
                operation=f"Could not inspect Settings for {expected_text}",
                timeout=30,
            )
            result = self._adb_binary_command(
                serial, "exec-out", "cat", remote_dump, timeout=20)
            if result[0] != 0:
                detail = result[2].decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"Could not read Settings UI for {expected_text}: "
                    + (detail or f"exit code {result[0]}"))
            x, y = self._ui_text_center(result[1], expected_text)
            self._checked_adb(
                "shell", "input", "tap", str(x), str(y),
                serial=serial,
                operation=f"Could not open Settings item {expected_text}",
            )
        finally:
            legacy.adb_command(
                "shell", "rm", "-f", remote_dump,
                serial=serial, timeout=15, telemetry=False)

    def _navigate_settings_display(self, serial):
        output = self._checked_adb(
            "shell", "am", "start", "-n", AI_SETTINGS_COMPONENT,
            serial=serial,
            operation="Could not reopen Settings",
            timeout=30,
        )
        if "error" in output.casefold():
            raise RuntimeError(f"Could not reopen Settings: {output}")
        time.sleep(2.0)
        self._tap_settings_text(serial, "Device Preferences")
        time.sleep(2.0)
        self._tap_settings_text(serial, "Display")
        time.sleep(2.0)

    def _synchronize_ai_preferences_off(
            self, serial, root_mode, *, refresh_for_runtime_change=False):
        preferences_path = self._resolve_ai_preferences_path(serial, root_mode)
        backup_path = f"{preferences_path}.bak"
        if self._root_path_exists(
                serial, root_mode, backup_path):
            raise RuntimeError(
                "Settings preference backup exists; cache synchronization aborted")

        source_xml = self._read_root_file_bytes(
            serial, root_mode, preferences_path)
        try:
            initial_sync_result = sync_ai_display_preferences_off(source_xml)
        except ValueError as exc:
            raise RuntimeError(f"Unsafe Settings preference XML: {exc}") from exc
        if not initial_sync_result.changed and not refresh_for_runtime_change:
            return ()

        process_ids = self._settings_process_ids(serial)
        was_foreground = (
            self._settings_is_foreground(serial) if process_ids else False)

        stopped = False
        operation_error = None
        try:
            if process_ids:
                self._force_stop_settings(serial)
                stopped = True
            if self._root_path_exists(
                    serial, root_mode, backup_path):
                raise RuntimeError(
                    "Settings preference backup appeared; cache synchronization aborted")

            # Re-read only after the Settings process is confirmed stopped.
            # This prevents an unrelated SharedPreferences update made between
            # the initial drift check and force-stop from being overwritten by
            # an older snapshot.
            source_xml = self._read_root_file_bytes(
                serial, root_mode, preferences_path)
            try:
                sync_result = sync_ai_display_preferences_off(source_xml)
            except ValueError as exc:
                raise RuntimeError(
                    f"Unsafe Settings preference XML after stop: {exc}"
                ) from exc
            if sync_result.changed:
                self._write_root_preferences_atomically(
                    serial, root_mode, sync_result.xml_data, preferences_path)

            verified_xml = self._read_root_file_bytes(
                serial, root_mode, preferences_path)
            try:
                verified_result = sync_ai_display_preferences_off(verified_xml)
            except ValueError as exc:
                raise RuntimeError(
                    f"Could not validate synchronized Settings preferences: {exc}"
                ) from exc
            if verified_result.changed:
                raise RuntimeError(
                    "Settings preference verification still reports enabled controls")
            if verified_xml != sync_result.xml_data:
                raise RuntimeError(
                    "Settings preference verification did not match the exact "
                    "validated payload")
        except Exception as exc:
            operation_error = exc

        navigation_error = None
        if stopped and was_foreground:
            try:
                self._navigate_settings_display(serial)
            except Exception as exc:
                navigation_error = exc

        if operation_error is not None:
            if navigation_error is not None:
                self.logline(
                    f"[AI-PQ] Settings reopen also failed: {navigation_error}")
            raise operation_error
        if navigation_error is not None:
            raise navigation_error

        if sync_result.changed_keys:
            self.logline(
                "[AI-PQ] Settings preference cache synchronized off: "
                + ", ".join(sync_result.changed_keys))
        return sync_result.changed_keys

    def _ai_pq_watchdog(self):
        if self._display_protection_enabled and self.active_serial():
            self.enforce_ai_pq_off(silent=True)

    @staticmethod
    def _supported_property_keys(plan):
        return {item.property_key for item in plan.supported_properties}

    @staticmethod
    def _protected_plan_status(plan):
        supported_count = len(plan.supported_toggle_specs)
        if plan.capability is CapabilityLevel.FULL:
            return "Protected · 7/7 controls off", "online"
        if plan.capability is CapabilityLevel.PARTIAL:
            return (
                f"Protected · {supported_count}/7 supported controls off",
                "pending",
            )
        return "Not available on this firmware", "idle"

    @staticmethod
    def _short_error(error, limit=180):
        detail = " ".join(str(error).split()) or "unknown error"
        return detail if len(detail) <= limit else detail[:limit - 1] + "…"

    def _record_guard_failure(self, serial, reason):
        reason = self._short_error(reason)
        self._guard_status_by_serial[serial] = "failed"
        self._guard_failure_reason_by_serial[serial] = reason
        self.logline(f"[AI-PQ] Protection failed on {serial}: {reason}")
        self.ui(self._set_ai_pq_status, f"Protection failed · {reason}", "error")

    def _read_ai_preference_booleans(self, serial, root_mode):
        preferences_path = self._resolve_ai_preferences_path(serial, root_mode)
        xml_data = self._read_root_file_bytes(serial, root_mode, preferences_path)
        try:
            values = parse_shared_preferences_booleans(xml_data)
        except ValueError as exc:
            raise RuntimeError(f"Unsupported Settings preference schema: {exc}") from exc
        missing = [
            spec.preference_key
            for spec in AI_DISPLAY_TOGGLES
            if spec.preference_key not in values
        ]
        if missing:
            raise RuntimeError(
                "Settings preference schema is missing: " + ", ".join(missing))
        return preferences_path, values

    def _stop_vendor_pq_service(self, serial, root_mode):
        service = legacy.adb_command(
            "shell", "getprop", "init.svc.rkswpq_demo",
            serial=serial, timeout=15, telemetry=False)
        state = self._adb_result_output(service).casefold()
        if service[0] != 0 or not state or state == "stopped":
            return
        self._checked_root_adb(
            serial, root_mode, "stop", "rkswpq_demo",
            operation="Could not stop vendor picture-processing service")
        time.sleep(0.35)
        check = legacy.adb_command(
            "shell", "getprop", "init.svc.rkswpq_demo",
            serial=serial, timeout=15, telemetry=False)
        if self._adb_result_output(check).casefold() == "running":
            raise RuntimeError("Vendor picture-processing service remained running")

    def _pulse_vendor_pipeline(self, serial, root_mode, raw_values):
        update_key = "vendor.tvinput.rkpq.update_vdpp_cfg"
        if not str(raw_values.get(update_key, "")).strip():
            return
        for value in ("0", "1"):
            result = legacy.adb_command(
                *self._root_setprop_args(root_mode, update_key, value),
                serial=serial, timeout=20, telemetry=False)
            if result[0] != 0:
                detail = self._adb_result_output(result) or f"exit code {result[0]}"
                raise RuntimeError(f"Display pipeline update pulse failed: {detail}")
            if value == "0":
                time.sleep(0.25)

    def _apply_runtime_off_baseline(
            self, serial, root_mode, properties_to_apply, initial_values):
        failures = {}
        for name, expected in properties_to_apply.items():
            result = legacy.adb_command(
                *self._root_setprop_args(root_mode, name, expected),
                serial=serial, timeout=20, telemetry=False)
            if result[0] != 0:
                failures[name] = (
                    self._adb_result_output(result) or f"exit code {result[0]}")
            time.sleep(0.15)
        if failures:
            name, detail = next(iter(failures.items()))
            raise RuntimeError(f"Property write denied for {name}: {detail}")

        self._pulse_vendor_pipeline(serial, root_mode, initial_values)
        time.sleep(0.8)
        verify_result, verified_values = self._read_ai_pq_state(serial)
        if verify_result[0] != 0:
            detail = self._adb_result_output(verify_result) or "ADB/tunnel read failed"
            raise RuntimeError(f"Runtime verification failed: {detail}")

        remaining = {
            name: expected
            for name, expected in properties_to_apply.items()
            if str(verified_values.get(name, "")).strip() != expected
        }
        if remaining:
            for name, expected in remaining.items():
                result = legacy.adb_command(
                    *self._root_setprop_args(root_mode, name, expected),
                    serial=serial, timeout=20, telemetry=False)
                if result[0] != 0:
                    detail = self._adb_result_output(result) or f"exit code {result[0]}"
                    raise RuntimeError(f"Property retry denied for {name}: {detail}")
                time.sleep(0.15)
            self._pulse_vendor_pipeline(serial, root_mode, verified_values)
            time.sleep(0.8)
            verify_result, verified_values = self._read_ai_pq_state(serial)
            if verify_result[0] != 0:
                raise RuntimeError("ADB/tunnel failed during property retry verification")
            remaining = {
                name: expected
                for name, expected in remaining.items()
                if str(verified_values.get(name, "")).strip() != expected
            }
            if remaining:
                raise RuntimeError(
                    "Firmware ignored property correction: " + ", ".join(remaining))

        time.sleep(2.0)
        stable_result, stable_values = self._read_ai_pq_state(serial)
        if stable_result[0] != 0:
            raise RuntimeError("ADB/tunnel failed during stability verification")
        reverted = [
            name
            for name, expected in properties_to_apply.items()
            if str(stable_values.get(name, "")).strip() != expected
        ]
        if reverted:
            raise RuntimeError(
                "Firmware reverted property after verification: " + ", ".join(reverted))
        return plan_guard_on_connection(serial, stable_values), stable_values

    def verify_ai_pq_state(self):
        serial = self.require_serial()
        if not serial:
            return
        if not self._ai_guard_lock.acquire(blocking=False):
            self._set_ai_pq_status("Protection update already in progress", "pending")
            return
        self._set_ai_pq_status("Verifying runtime and rooted Settings cache…", "pending")

        def job():
            try:
                result, values = self._read_ai_pq_state(serial)
                if result[0] != 0:
                    detail = self._adb_result_output(result) or "ADB/tunnel read failed"
                    self._record_guard_failure(serial, detail)
                    return
                plan = plan_guard_on_connection(serial, values)
                if not plan.compatible:
                    self._guard_status_by_serial[serial] = "not_applicable"
                    self.ui(
                        self._set_ai_pq_status,
                        "Not applicable · supported display controls were not found",
                        "idle")
                    return
                root_mode = self._root_access_mode(serial)
                if not root_mode:
                    self._record_guard_failure(
                        serial,
                        self._root_failure_reason_by_serial.get(
                            serial, "Root access unavailable"))
                    return
                try:
                    _path, preference_values = self._read_ai_preference_booleans(
                        serial, root_mode)
                except Exception as exc:
                    self._record_guard_failure(serial, exc)
                    return
                evaluation = evaluate_getprop_mapping(values)
                self.ui(
                    self._render_ai_evaluation,
                    evaluation,
                    self._supported_property_keys(plan),
                    preference_values,
                )
                cached_on = [name for name, enabled in preference_values.items() if enabled]
                if plan.correction_map or cached_on:
                    parts = []
                    if plan.correction_map:
                        parts.append(f"{len(plan.correction_map)} runtime")
                    if cached_on:
                        parts.append(f"{len(cached_on)} Settings cache")
                    reason = "Mismatch detected · " + " + ".join(parts)
                    self._guard_status_by_serial[serial] = "failed"
                    self._guard_failure_reason_by_serial[serial] = reason
                    self.ui(self._set_ai_pq_status, reason, "error")
                    return
                self._guard_status_by_serial[serial] = "protected"
                self._guard_failure_reason_by_serial.pop(serial, None)
                self.ui(
                    self._set_ai_pq_status,
                    "Verified · runtime and Settings cache are off",
                    "online")
            finally:
                self._ai_guard_lock.release()

        self.worker(job)

    def enforce_ai_pq_off(
            self, silent=False, on_complete=None, apply_all=False, force=False):
        # ``force`` is the operator pressing the button themselves. The
        # display-protection toggle governs whether this runs automatically on
        # connect; it must not veto an explicit request.
        if apply_all and not force and not self._display_protection_enabled:
            if on_complete:
                self.ui(on_complete, True)
            return
        serial = self.active_serial()
        if not serial:
            if not silent:
                self.require_serial()
            if on_complete:
                self.ui(on_complete, False)
            return
        if not self._ai_guard_lock.acquire(blocking=False):
            if (on_complete or apply_all) and not self._closing:
                self.after(
                    250, self.enforce_ai_pq_off,
                    silent, on_complete, apply_all, force)
            return
        if not silent:
            self._set_ai_pq_status("Enforcing rooted display baseline…", "pending")

        def job():
            success = False
            try:
                if apply_all and not self._display_protection_enabled:
                    success = True
                    return
                result, values = self._read_ai_pq_state(serial)
                if result[0] != 0:
                    detail = self._adb_result_output(result) or "ADB/tunnel read failed"
                    self._record_guard_failure(serial, detail)
                    return
                plan = plan_guard_on_connection(serial, values)
                evaluation = evaluate_getprop_mapping(values)
                self.ui(
                    self._render_ai_evaluation,
                    evaluation,
                    self._supported_property_keys(plan),
                )
                if not plan.compatible:
                    self._guard_status_by_serial[serial] = "not_applicable"
                    self._guard_failure_reason_by_serial.pop(serial, None)
                    self.ui(
                        self._set_ai_pq_status,
                        "Not applicable · supported display controls were not found",
                        "idle")
                    success = True
                    return

                if apply_all and not self._display_protection_enabled:
                    success = True
                    return

                root_mode = self._root_access_mode(serial)
                if not root_mode:
                    self._record_guard_failure(
                        serial,
                        self._root_failure_reason_by_serial.get(
                            serial, "Root access unavailable"))
                    return

                try:
                    self._stop_vendor_pq_service(serial, root_mode)
                    properties_to_apply = (
                        plan.application_map if apply_all else plan.correction_map)
                    verified_plan = plan
                    verified_values = values
                    if properties_to_apply:
                        self.ui(
                            self._set_ai_pq_status,
                            f"Applying {len(properties_to_apply)} rooted setting(s)…",
                            "pending")
                        verified_plan, verified_values = self._apply_runtime_off_baseline(
                            serial, root_mode, properties_to_apply, values)

                    self._synchronize_ai_preferences_off(
                        serial,
                        root_mode,
                        refresh_for_runtime_change=(
                            apply_all or bool(plan.correction_map)),
                    )
                    _path, preference_values = self._read_ai_preference_booleans(
                        serial, root_mode)
                    final_result, final_values = self._read_ai_pq_state(serial)
                    if final_result[0] != 0:
                        raise RuntimeError(
                            "ADB/tunnel failed during final rooted verification")
                    verified_plan = plan_guard_on_connection(serial, final_values)
                    cached_on = [
                        name for name, enabled in preference_values.items() if enabled]
                    if verified_plan.correction_map:
                        raise RuntimeError(
                            "Runtime values are not off: "
                            + ", ".join(verified_plan.correction_map))
                    if cached_on:
                        raise RuntimeError(
                            "Settings cache remained on: " + ", ".join(cached_on))
                except Exception as exc:
                    self._record_guard_failure(serial, exc)
                    return

                final_evaluation = evaluate_getprop_mapping(final_values)
                self.ui(
                    self._render_ai_evaluation,
                    final_evaluation,
                    self._supported_property_keys(verified_plan),
                    preference_values,
                )
                self._guard_status_by_serial[serial] = "protected"
                self._guard_failure_reason_by_serial.pop(serial, None)
                status, tone = self._protected_plan_status(verified_plan)
                self.ui(self._set_ai_pq_status, status, tone)
                self.logline(
                    f"[AI-PQ] {serial}: runtime, rooted Settings cache and "
                    "stability read-back verified off.")
                success = True
            finally:
                self._ai_guard_lock.release()
                if on_complete:
                    self.ui(on_complete, success)

        self.worker(job)

    def closeEvent(self, event):
        self._save_layout()
        super().closeEvent(event)
        if event.isAccepted():
            self._closing = True
            self._mirror_generation += 1
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        else:
            self._closing = False


def main():
    v1._startup_trace("V2 QApplication construction started")
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Convrse.DeviceControl.V2.3.4")
        except Exception:
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Convrse")
    app.setWindowIcon(QIcon(v1.resource_path("assets/convrse-logo.png")))
    app.setStyle("Fusion")
    app.setStyleSheet(V2_STYLESHEET)
    window = CdcV2Window()
    window.showMaximized()
    window.raise_()
    window.activateWindow()
    v1._startup_trace("V2 main window shown maximized")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

