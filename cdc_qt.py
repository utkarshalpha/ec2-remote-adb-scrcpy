#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PySide6 desktop shell for Convrse Device Control.

The device operations remain shared with the proven CDC implementation while
Qt owns presentation, scheduling, dialogs, window embedding, and thread-safe UI
delivery.  This keeps the operational behavior stable and removes Tk's layout
and native-window limitations.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime


def _startup_trace(message):
    """Leave a small frozen-startup trail beside the EXE for field support."""
    if not getattr(sys, "frozen", False):
        return
    try:
        path = os.path.join(
            os.path.dirname(sys.executable), "Convrse-Device-Control-startup.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S.%f}  {message}\n")
    except OSError:
        pass


_startup_trace("Python entry loaded")

# The packaged Windows build must always use the native platform plugin.  This
# also shields the release from inherited test/automation environments that
# silently select Qt's non-windowed offscreen backend.
if getattr(sys, "frozen", False) and os.name == "nt":
    os.environ["QT_QPA_PLATFORM"] = "windows"

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QIcon, QKeySequence, QPixmap, QShortcut, QTextCharFormat,
    QTextCursor, QWindow,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

_startup_trace("PySide6 imports complete")

import scrcpy_remote as legacy

_startup_trace("CDC operations module imported")


APP_NAME = legacy.APP_NAME
APP_SHORT_NAME = legacy.APP_SHORT_NAME
APP_VERSION = "V1.1"

BG = "#070C16"
BG_ALT = "#0B1220"
SIDEBAR = "#0C1423"
SURFACE = "#121D30"
SURFACE_2 = "#17243A"
SURFACE_3 = "#1C2C45"
BORDER = "#243653"
TEXT = "#F3F7FC"
MUTED = "#95A7C2"
SUBTLE = "#657A99"
BLUE = "#64A2FF"
BLUE_HOVER = "#82B5FF"
GREEN = "#43D6A3"
AMBER = "#F2C45E"
RED = "#FF7081"

AI_PQ_PROPERTIES = {
    "persist.vendor.rkpq.dc.enable": "0",
    "persist.vendor.rkpq.fe.enable": "0",
    "persist.vendor.rkpq.hwpq_aisd_enable": "0",
    "persist.vendor.rkpq.hwpq_lce_ratio": "0",
    "persist.vendor.rkpq.hwpq_shp_en": "0",
    "persist.vendor.rkpq.iptv_sr_enable": "0",
    "persist.vendor.rkpq.memc.enable": "0",
    "persist.vendor.rkpq.memc.stp.enable": "0",
    "persist.vendor.rkpq.sr.enable": "0",
    "vendor.hwc.hwpq_force_enable": "0",
    "vendor.tvinput.rkpq.vdpp_shp_en": "0",
}


def resource_path(relative_path):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


APP_STYLESHEET = f"""
* {{
    font-family: "Segoe UI";
    font-size: 12px;
    color: {TEXT};
}}
QMainWindow, QWidget#Root {{ background: {BG}; }}
QWidget#Header, QWidget#Footer {{ background: {BG_ALT}; }}
QFrame#Sidebar {{ background: {SIDEBAR}; border: 1px solid #13213A; }}
QFrame#Card, QFrame#StageToolbar, QFrame#Console {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QLabel#Brand {{
    background: {BLUE}; color: {BG}; font-size: 14px; font-weight: 800;
    border-radius: 6px; padding: 9px 12px;
}}
QLabel#Logo {{ background: white; border-radius: 8px; padding: 3px; }}
QLabel#AppTitle {{ font-size: 20px; font-weight: 700; color: {TEXT}; }}
QLabel#AppSubtitle, QLabel#Overline {{
    color: {BLUE}; font-size: 10px; font-weight: 700; letter-spacing: 1px;
}}
QLabel#CardTitle {{ font-size: 14px; font-weight: 700; color: {TEXT}; }}
QLabel#FieldLabel {{
    color: {SUBTLE}; font-size: 9px; font-weight: 700; letter-spacing: 0.8px;
}}
QLabel#Secondary {{ color: {MUTED}; }}
QLabel#Tertiary {{ color: {SUBTLE}; font-size: 10px; }}
QLabel#StageTitle {{ font-size: 18px; font-weight: 700; }}
QLabel#EmptyTitle {{ font-size: 21px; font-weight: 700; }}
QLabel#Package {{
    background: {BG_ALT}; border: 1px solid #1C2C46; border-radius: 5px;
    color: {MUTED}; font-family: "Cascadia Mono"; font-size: 10px; padding: 8px;
}}
QLabel#TunnelPill {{
    background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 7px;
    color: {MUTED}; padding: 8px 12px; font-weight: 700;
}}
QLabel#TunnelPill[tone="online"] {{ color: {GREEN}; border-color: #245B50; }}
QLabel#TunnelPill[tone="pending"] {{ color: {AMBER}; border-color: #5B4C27; }}
QLabel#TunnelPill[tone="error"] {{ color: {RED}; border-color: #633044; }}
QLabel#StatusDot {{ color: {GREEN}; font-size: 10px; }}
QLabel#AiStatus {{
    background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 6px;
    color: {MUTED}; padding: 9px; font-weight: 700;
}}
QLabel#AiStatus[tone="online"] {{ color: {GREEN}; border-color: #245B50; }}
QLabel#AiStatus[tone="pending"] {{ color: {AMBER}; border-color: #5B4C27; }}
QLabel#AiStatus[tone="error"] {{ color: {RED}; border-color: #633044; }}
QLineEdit, QComboBox {{
    background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 5px;
    color: {TEXT}; padding: 7px 9px; min-height: 19px;
}}
QLineEdit:focus, QComboBox:focus {{ border: 1px solid {BLUE}; }}
QComboBox::drop-down {{ border: 0; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_2}; border: 1px solid {BORDER}; selection-background-color: {SURFACE_3};
}}
QPushButton {{
    background: {SURFACE_2}; border: 1px solid {SURFACE_3}; border-radius: 7px;
    color: {TEXT}; padding: 7px 11px; min-height: 22px;
}}
QPushButton:hover {{ background: {SURFACE_3}; border-color: #395171; }}
QPushButton:pressed {{ background: #213451; }}
QPushButton:disabled {{ color: {SUBTLE}; background: #101A2B; border-color: #18263C; }}
QPushButton[role="primary"] {{
    background: {BLUE}; color: {BG}; border-color: {BLUE}; font-weight: 700;
}}
QPushButton[role="primary"]:hover {{ background: {BLUE_HOVER}; border-color: {BLUE_HOVER}; }}
QPushButton[role="danger"] {{ background: #462333; color: #FFB6C0; border-color: #673247; }}
QPushButton[role="danger"]:hover {{ background: {RED}; color: {BG}; border-color: {RED}; }}
QPushButton[role="quiet"] {{ background: transparent; color: {MUTED}; border-color: {BORDER}; }}
QPushButton[role="nav"] {{
    background: transparent; color: {MUTED}; border: 0; border-radius: 6px;
    padding: 8px 10px; font-weight: 600;
}}
QPushButton[role="nav"]:hover {{ background: {SURFACE_2}; color: {TEXT}; }}
QPushButton[role="nav"][active="true"] {{ background: {BLUE}; color: {BG}; }}
QPushButton#RemoteButton {{ min-height: 27px; }}
QFrame#CategoryNav {{
    background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 8px;
}}
QScrollArea {{ background: transparent; border: 0; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #314563; border-radius: 4px; min-height: 34px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:vertical {{ height: 8px; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QTextEdit#CommandConsole {{
    background: #050910; border: 0; border-radius: 6px; color: #B9C8DD;
    font-family: "Cascadia Mono"; font-size: 11px; padding: 8px;
}}
"""


def _set_role(widget, role):
    widget.setProperty("role", role)
    return widget


def _label(text, object_name=None):
    widget = QLabel(text)
    if object_name:
        widget.setObjectName(object_name)
    return widget


class UiBridge(QObject):
    requested = Signal(object, object, object)


class ValueAdapter:
    """Small compatibility adapter for the legacy StringVar surface."""

    def __init__(self, getter, setter):
        self._getter = getter
        self._setter = setter

    def get(self):
        return self._getter()

    def set(self, value):
        self._setter(str(value))


class ButtonAdapter:
    def __init__(self, button):
        self.widget = button

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.widget.setText(str(kwargs["text"]))
        if "state" in kwargs:
            self.widget.setEnabled(kwargs["state"] not in ("disabled", False))


class ComboAdapter:
    def __init__(self, combo):
        self.widget = combo

    def __setitem__(self, key, value):
        if key != "values":
            raise KeyError(key)
        current = self.widget.currentText()
        self.widget.blockSignals(True)
        self.widget.clear()
        self.widget.addItems([str(item) for item in value])
        index = self.widget.findText(current)
        self.widget.setCurrentIndex(index)
        self.widget.blockSignals(False)


class QtMessageBoxAdapter:
    @staticmethod
    def _parent():
        return QApplication.activeWindow()

    @classmethod
    def showinfo(cls, title, message, **_kwargs):
        QMessageBox.information(cls._parent(), title, message)

    @classmethod
    def showwarning(cls, title, message, **_kwargs):
        QMessageBox.warning(cls._parent(), title, message)

    @classmethod
    def showerror(cls, title, message, **_kwargs):
        QMessageBox.critical(cls._parent(), title, message)

    @classmethod
    def askyesno(cls, title, message, **_kwargs):
        result = QMessageBox.question(
            cls._parent(), title, message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes


class QtFileDialogAdapter:
    @staticmethod
    def _filter(filetypes):
        return ";;".join(f"{label} ({pattern})" for label, pattern in (filetypes or []))

    @classmethod
    def askopenfilename(cls, title="Open", filetypes=None, **_kwargs):
        path, _selected = QFileDialog.getOpenFileName(
            QApplication.activeWindow(), title, "", cls._filter(filetypes))
        return path

    @classmethod
    def askopenfilenames(cls, title="Open", filetypes=None, **_kwargs):
        paths, _selected = QFileDialog.getOpenFileNames(
            QApplication.activeWindow(), title, "", cls._filter(filetypes))
        return tuple(paths)

    @classmethod
    def asksaveasfilename(cls, title="Save", initialfile="", defaultextension="",
                          filetypes=None, **_kwargs):
        suggested = initialfile
        path, _selected = QFileDialog.getSaveFileName(
            QApplication.activeWindow(), title, suggested, cls._filter(filetypes))
        if path and defaultextension and not os.path.splitext(path)[1]:
            path += defaultextension
        return path


class CommandConsole(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Console")
        self.setMinimumHeight(145)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 7, 9, 9)
        layout.setSpacing(6)

        header = QHBoxLayout()
        live = _label("●  LIVE", "Overline")
        header.addWidget(live)
        header.addStretch(1)

        copy_button = _set_role(QPushButton("Copy"), "quiet")
        copy_button.clicked.connect(self.copy_all)
        export_button = _set_role(QPushButton("Save"), "quiet")
        export_button.clicked.connect(self.export_log)
        clear_button = _set_role(QPushButton("Clear"), "quiet")
        clear_button.clicked.connect(self.clear)
        header.addWidget(copy_button)
        header.addWidget(export_button)
        header.addWidget(clear_button)
        layout.addLayout(header)

        self.output = QTextEdit()
        self.output.setObjectName("CommandConsole")
        self.output.setReadOnly(True)
        self.output.document().setMaximumBlockCount(12000)
        layout.addWidget(self.output, 1)

    def append_message(self, message, color=None):
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        char_format = QTextCharFormat()
        char_format.setForeground(QColor(color or "#B9C8DD"))
        cursor.setCharFormat(char_format)
        timestamp = datetime.now().strftime("%H:%M:%S")
        cursor.insertText(f"{timestamp}  {message.rstrip()}\n")
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def copy_all(self):
        QApplication.clipboard().setText(self.output.toPlainText())

    def clear(self):
        self.output.clear()

    def export_log(self):
        default = f"CDC-command-log-{datetime.now():%Y%m%d-%H%M%S}.txt"
        path, _selected = QFileDialog.getSaveFileName(
            self, "Export command console", default, "Text log (*.txt)")
        if path:
            if not os.path.splitext(path)[1]:
                path += ".txt"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.output.toPlainText())


class MirrorHost(QFrame):
    """Responsive host for the foreign scrcpy window and its empty state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MirrorHost")
        self.setStyleSheet(
            f"QFrame#MirrorHost {{ background: #02050A; border: 1px solid {BORDER}; "
            "border-radius: 8px; }}")
        self.setMinimumSize(420, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(1, 1, 1, 1)
        self.foreign_window = None
        self.foreign_container = None

        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.addStretch(1)
        overline = _label("NO ACTIVE MIRROR", "Overline")
        title = _label("Your device will appear here", "EmptyTitle")
        detail = _label(
            "Connect a device, choose a stream profile, then start the mirror.",
            "Secondary")
        for widget in (overline, title, detail):
            widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_layout.addWidget(widget)
        empty_layout.addStretch(1)
        self.layout_root.addWidget(self.empty_state)

    def adopt_window(self, hwnd):
        self.clear_window()
        self.foreign_window = QWindow.fromWinId(int(hwnd))
        if self.foreign_window is None:
            raise RuntimeError("Qt could not wrap the scrcpy window")
        self.foreign_container = QWidget.createWindowContainer(
            self.foreign_window, self, Qt.WindowType.FramelessWindowHint)
        self.foreign_container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.layout_root.addWidget(self.foreign_container)
        self.empty_state.hide()

    def clear_window(self):
        if self.foreign_container is not None:
            self.layout_root.removeWidget(self.foreign_container)
            self.foreign_container.deleteLater()
        self.foreign_container = None
        self.foreign_window = None
        self.empty_state.show()


class OperationsMixin:
    """Receives the battle-tested non-visual CDC operations at module load."""


_UI_METHODS = {
    "__init__", "_enable_dark_titlebar", "_build_style", "_build_dashboard_ui",
    "_sidebar_group", "_section_connection", "_section_remote",
    "_section_app_recovery", "_section_utilities", "_section_stream",
    "_section_status", "toggle_log", "_set_tunnel_state", "ui", "worker",
    "logline", "_adopt_scrcpy_window", "_resize_embedded_mirror",
    "_clear_mirror_embed", "on_close",
}
for _method_name, _method in legacy.ScrcpyRemote.__dict__.items():
    if _method_name not in _UI_METHODS and (
            callable(_method) or isinstance(_method, (staticmethod, classmethod))):
        setattr(OperationsMixin, _method_name, _method)


class CdcMainWindow(OperationsMixin, QMainWindow):
    def __init__(self):
        _startup_trace("Main window construction started")
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · {APP_VERSION}")
        self.setWindowIcon(QIcon(resource_path("assets/convrse-logo.png")))
        self.resize(1500, 960)
        self.setMinimumSize(1024, 700)

        self.devices = []
        self.scrcpy_proc = None
        self.ssh_proc = None
        self.ssh_port = None
        self.ssh_pem = None
        self.capture_proc = None
        self.capture_handle = None
        self.capture_dir = None
        self.capture_package = None
        self.capture_serial = None
        self.capture_pids = set()
        self.capture_started = None
        self.capture_deadline = None
        self.capture_packages = set()
        self.capture_stop_event = threading.Event()
        self.capture_metrics_handle = None
        self.capture_metrics_thread = None
        self.capture_input_proc = None
        self.capture_input_handle = None
        self.capture_timer_id = None
        self.last_network_totals = {}
        self.input_action_count = 0
        self.scrcpy_hwnd = None
        self.mirror_aspect = None
        self.session_touch_downs = 0
        self.session_tracking_downs = 0
        self.session_clicks = 0
        self.session_keys = 0
        self._timers = {}
        self._timer_sequence = 0
        self._ai_guard_lock = threading.Lock()
        self._ai_guard_last_serial = None

        self._bridge = UiBridge(self)
        self._bridge.requested.connect(
            self._execute_ui, Qt.ConnectionType.QueuedConnection)

        _startup_trace("Main window state initialized")
        self._build_ui()
        _startup_trace("Main window UI built")
        self._install_adapters()
        _startup_trace("Main window adapters installed")
        legacy.messagebox = QtMessageBoxAdapter
        legacy.filedialog = QtFileDialogAdapter
        legacy.set_command_observer(self._observe_command)

        self.ai_guard_timer = QTimer(self)
        self.ai_guard_timer.setInterval(12000)
        self.ai_guard_timer.timeout.connect(self._ai_pq_watchdog)
        self.ai_guard_timer.start()

        self.logline("[CDC] Qt operator console ready. Command telemetry is live.")
        self.after(150, self.refresh_devices)
        _startup_trace("Main window construction complete")

    # ---------- Qt UI ----------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        body.setHandleWidth(8)
        body.addWidget(self._build_sidebar())
        body.addWidget(self._build_workspace())
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([385, 1115])
        outer.addWidget(body, 1)
        self.main_splitter = body

    def _build_header(self):
        header = QWidget()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 13, 18, 13)
        layout.setSpacing(13)
        logo = _label("", "Logo")
        logo.setFixedSize(50, 50)
        pixmap = QPixmap(resource_path("assets/convrse-logo.png"))
        logo.setPixmap(pixmap.scaled(
            42, 42, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        titles.addWidget(_label(APP_NAME, "AppTitle"))
        titles.addWidget(_label("DEVICE OPERATIONS  ·  V1", "AppSubtitle"))
        layout.addLayout(titles)
        layout.addStretch(1)

        self.status_label = _label("Ready", "Secondary")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setMaximumWidth(360)
        layout.addWidget(self.status_label)

        status_wrap = QVBoxLayout()
        status_wrap.setSpacing(3)
        secure = _label("SECURE TUNNEL", "FieldLabel")
        secure.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_wrap.addWidget(secure)
        self.tunnel_label = _label("Disconnected", "TunnelPill")
        self.tunnel_label.setProperty("tone", "idle")
        self.tunnel_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_wrap.addWidget(self.tunnel_label)
        layout.addLayout(status_wrap)
        return header

    def _build_sidebar(self):
        shell = QFrame()
        shell.setObjectName("Sidebar")
        shell.setMinimumWidth(340)
        shell.setMaximumWidth(430)
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(8, 8, 4, 8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("SidebarContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 5, 2)
        content_layout.setSpacing(8)
        content_layout.addWidget(self._connection_card())

        category_nav = QFrame()
        category_nav.setObjectName("CategoryNav")
        nav_layout = QHBoxLayout(category_nav)
        nav_layout.setContentsMargins(4, 4, 4, 4)
        nav_layout.setSpacing(3)
        self.category_buttons = []
        self.category_group = QButtonGroup(self)
        self.category_group.setExclusive(True)
        for index, title in enumerate(("Remote", "Recovery", "Device")):
            button = _set_role(QPushButton(title), "nav")
            button.setCheckable(True)
            button.setProperty("active", index == 0)
            button.clicked.connect(lambda _checked=False, page=index: self._show_category(page))
            self.category_group.addButton(button, index)
            self.category_buttons.append(button)
            nav_layout.addWidget(button, 1)
        self.category_buttons[0].setChecked(True)
        content_layout.addWidget(category_nav)

        self.category_stack = QStackedWidget()
        self.category_stack.addWidget(self._category_page(self._remote_card()))
        self.category_stack.addWidget(self._category_page(self._recovery_card()))
        self.category_stack.addWidget(self._category_page(
            self._ai_pq_card(), self._tools_card()))
        content_layout.addWidget(self.category_stack)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        shell_layout.addWidget(scroll)
        self.sidebar_scroll = scroll
        return shell

    def _category_page(self, *cards):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for card in cards:
            layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _show_category(self, index):
        self.category_stack.setCurrentIndex(index)
        for button_index, button in enumerate(self.category_buttons):
            button.setProperty("active", button_index == index)
            button.style().unpolish(button)
            button.style().polish(button)

    def _card(self, title):
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(11, 10, 11, 11)
        layout.setSpacing(8)
        layout.addWidget(_label(title, "CardTitle"))
        return card, layout

    def _connection_card(self):
        card, layout = self._card("Device connection")
        layout.addWidget(_label("SSH PRIVATE KEY", "FieldLabel"))
        key_row = QHBoxLayout()
        self.pem_edit = QLineEdit()
        self.pem_edit.setPlaceholderText("Select a .pem private key")
        browse = _set_role(QPushButton("Browse"), "quiet")
        browse.clicked.connect(self.select_pem_key)
        key_row.addWidget(self.pem_edit, 1)
        key_row.addWidget(browse)
        layout.addLayout(key_row)

        endpoint = QGridLayout()
        endpoint.setHorizontalSpacing(8)
        endpoint.setVerticalSpacing(4)
        endpoint.addWidget(_label("ADB PORT", "FieldLabel"), 0, 0)
        endpoint.addWidget(_label("LOCAL HOST", "FieldLabel"), 0, 1)
        self.port_edit = QLineEdit(legacy.DEFAULT_ADB_PORT)
        self.ip_edit = QLineEdit(legacy.DEFAULT_IP)
        self.ip_edit.setReadOnly(True)
        endpoint.addWidget(self.port_edit, 1, 0)
        endpoint.addWidget(self.ip_edit, 1, 1)
        layout.addLayout(endpoint)

        actions = QHBoxLayout()
        self.tunnel_button_widget = _set_role(QPushButton("Connect device"), "primary")
        self.tunnel_button_widget.clicked.connect(self.connect_tunnel)
        disconnect = _set_role(QPushButton("Disconnect"), "quiet")
        disconnect.clicked.connect(self.disconnect_tunnel)
        actions.addWidget(self.tunnel_button_widget, 2)
        actions.addWidget(disconnect, 1)
        layout.addLayout(actions)

        layout.addWidget(_label("ACTIVE DEVICE", "FieldLabel"))
        device_row = QHBoxLayout()
        self.device_combo = QComboBox()
        refresh = _set_role(QPushButton("Refresh"), "quiet")
        refresh.clicked.connect(self.refresh_devices)
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(refresh)
        layout.addLayout(device_row)
        layout.addWidget(_label(f"Gateway  {legacy.SSH_HOST}", "Tertiary"))
        return card

    def _remote_card(self):
        card, layout = self._card("Remote control")
        grid = QGridLayout()
        grid.setSpacing(4)
        buttons = [
            ("Back", lambda: self.send_keyevent("4")),
            ("Home", lambda: self.send_keyevent("3")),
            ("Menu", lambda: self.send_keyevent("82")),
            ("Power", lambda: self.send_keyevent("26")),
            ("Wake", lambda: self.send_keyevent("224")),
            ("Sleep", lambda: self.send_keyevent("223")),
            ("Volume −", lambda: self.send_keyevent("25")),
            ("Mute", lambda: self.send_keyevent("164")),
            ("Volume +", lambda: self.send_keyevent("24")),
            ("Previous", lambda: self.send_keyevent("88")),
            ("Play / Pause", lambda: self.send_keyevent("85")),
            ("Next", lambda: self.send_keyevent("87")),
            ("Enter / OK", lambda: self.send_keyevent("66")),
            ("Screenshot", self.screenshot),
            ("Settings", self.open_device_settings),
        ]
        for index, (title, callback) in enumerate(buttons):
            button = QPushButton(title)
            button.setObjectName("RemoteButton")
            button.clicked.connect(callback)
            grid.addWidget(button, index // 3, index % 3)
        layout.addLayout(grid)

        custom = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("Key code")
        self.key_edit.setMaximumWidth(100)
        send = QPushButton("Send key code")
        send.clicked.connect(self.send_custom_keyevent)
        custom.addWidget(self.key_edit)
        custom.addWidget(send, 1)
        layout.addLayout(custom)
        return card

    def _recovery_card(self):
        card, layout = self._card("App recovery")
        self.current_app_label = _label("Foreground app: detected automatically", "Package")
        self.current_app_label.setWordWrap(True)
        layout.addWidget(self.current_app_label)
        grid = QGridLayout()
        grid.setSpacing(4)
        controls = [
            ("Force Stop", self.force_stop_current, None),
            ("Restart App", self.restart_current, "primary"),
            ("Clear Cache", self.clear_current_cache, None),
            ("Clear Data", self.clear_current_data, "danger"),
            ("Close Background", self.close_background_apps, None),
            ("Export Logs", self.save_recent_logs, None),
        ]
        for index, (title, callback, role) in enumerate(controls):
            button = QPushButton(title)
            if role:
                _set_role(button, role)
            button.clicked.connect(callback)
            grid.addWidget(button, index // 3, index % 3)
        layout.addLayout(grid)
        return card

    def _tools_card(self):
        card, layout = self._card("Tools & diagnostics")
        grid = QGridLayout()
        grid.setSpacing(4)
        controls = [
            ("Install APK", self.install_apk),
            ("Open Store", self.open_convrse_store),
            ("Open CleanUp", self.open_cleanup_app),
            ("Launch Claude", self.run_claude),
        ]
        for index, (title, callback) in enumerate(controls):
            button = QPushButton(title)
            button.clicked.connect(callback)
            grid.addWidget(button, index // 2, index % 2)
        layout.addLayout(grid)
        self.capture_button_widget = _set_role(
            QPushButton("Start diagnostic session"), "primary")
        self.capture_button_widget.clicked.connect(self.toggle_debug_capture)
        layout.addWidget(self.capture_button_widget)
        return card

    def _ai_pq_card(self):
        card, layout = self._card("Display guard")
        self.ai_pq_status_label = _label("Waiting for a device", "AiStatus")
        self.ai_pq_status_label.setProperty("tone", "idle")
        self.ai_pq_status_label.setWordWrap(True)
        layout.addWidget(self.ai_pq_status_label)

        detail = _label(
            "Hardware AI-PQ, super-resolution, sharpening and MEMC remain off. "
            "V1 verifies device properties directly and corrects drift automatically.",
            "Tertiary")
        detail.setWordWrap(True)
        layout.addWidget(detail)

        actions = QGridLayout()
        actions.setSpacing(5)
        enforce = _set_role(QPushButton("Enforce all off"), "primary")
        enforce.clicked.connect(self.enforce_ai_pq_off)
        verify = _set_role(QPushButton("Verify now"), "quiet")
        verify.clicked.connect(self.verify_ai_pq_state)
        settings = _set_role(QPushButton("Display settings"), "quiet")
        settings.clicked.connect(self.open_display_settings)
        actions.addWidget(enforce, 0, 0, 1, 2)
        actions.addWidget(verify, 1, 0)
        actions.addWidget(settings, 1, 1)
        layout.addLayout(actions)
        return card

    def _build_workspace(self):
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        work = QWidget()
        work_layout = QVBoxLayout(work)
        work_layout.setContentsMargins(0, 0, 0, 0)
        work_layout.setSpacing(9)

        toolbar = QFrame()
        toolbar.setObjectName("StageToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(11, 7, 11, 7)
        toolbar_layout.setSpacing(8)
        toolbar_layout.addWidget(_label("●  LIVE VIEW", "Overline"))
        toolbar_layout.addStretch(1)
        self.session_status_label = _label("Diagnostics idle", "Tertiary")
        self.session_status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        toolbar_layout.addWidget(self.session_status_label)
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(list(legacy.STREAM_PROFILES))
        self.profile_combo.setCurrentText(legacy.DEFAULT_PROFILE)
        self.profile_combo.setMinimumWidth(205)
        self.start_button_widget = _set_role(QPushButton("Start mirror"), "primary")
        self.start_button_widget.clicked.connect(self.start_scrcpy)
        stop = _set_role(QPushButton("Stop"), "quiet")
        stop.clicked.connect(self.stop_scrcpy)
        toolbar_layout.addWidget(self.profile_combo)
        toolbar_layout.addWidget(self.start_button_widget)
        toolbar_layout.addWidget(stop)
        self.session_detail_label = _label("", "Tertiary")
        self.session_detail_label.hide()
        work_layout.addWidget(toolbar)

        self.mirror_frame = MirrorHost()
        work_layout.addWidget(self.mirror_frame, 1)
        splitter.addWidget(work)

        self.console = CommandConsole()
        splitter.addWidget(self.console)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([690, 190])
        self.workspace_splitter = splitter
        return splitter

    def _build_footer(self):
        footer = QWidget()
        footer.setObjectName("Footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(7)
        layout.addWidget(_label("●", "StatusDot"))
        self.status_label = _label("Ready", "Secondary")
        layout.addWidget(self.status_label)
        layout.addStretch(1)
        layout.addWidget(_label("F5  Refresh device   ·   Ctrl+L  Focus console", "Tertiary"))
        return footer

    def _install_adapters(self):
        self.pem_var = ValueAdapter(self.pem_edit.text, self.pem_edit.setText)
        self.port_var = ValueAdapter(self.port_edit.text, self.port_edit.setText)
        self.ip_var = ValueAdapter(self.ip_edit.text, self.ip_edit.setText)
        self.key_var = ValueAdapter(self.key_edit.text, self.key_edit.setText)
        self.device_var = ValueAdapter(self.device_combo.currentText, self._set_device)
        self.profile_var = ValueAdapter(self.profile_combo.currentText, self.profile_combo.setCurrentText)
        self.current_app_var = ValueAdapter(self.current_app_label.text, self.current_app_label.setText)
        self.status_var = ValueAdapter(self.status_label.text, self.status_label.setText)
        self.session_status_var = ValueAdapter(
            self.session_status_label.text, self.session_status_label.setText)
        self.session_detail_var = ValueAdapter(
            self.session_detail_label.text, self.session_detail_label.setText)
        self.tunnel_var = ValueAdapter(self.tunnel_label.text, self.tunnel_label.setText)
        self.tunnel_btn = ButtonAdapter(self.tunnel_button_widget)
        self.capture_btn = ButtonAdapter(self.capture_button_widget)
        self.start_btn = ButtonAdapter(self.start_button_widget)
        self.device_box = ComboAdapter(self.device_combo)
        self.device_combo.currentTextChanged.connect(self._device_changed)

        self.refresh_shortcut = QShortcut(QKeySequence("F5"), self)
        self.refresh_shortcut.activated.connect(self.refresh_devices)
        self.console_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self.console_shortcut.activated.connect(self.console.output.setFocus)

    def _set_device(self, value):
        index = self.device_combo.findText(value)
        self.device_combo.setCurrentIndex(index)

    # ---------- Rockchip AI-PQ guard ----------
    def _set_ai_pq_status(self, text, tone="idle"):
        self.ai_pq_status_label.setText(text)
        self.ai_pq_status_label.setProperty("tone", tone)
        self.ai_pq_status_label.style().unpolish(self.ai_pq_status_label)
        self.ai_pq_status_label.style().polish(self.ai_pq_status_label)

    @staticmethod
    def _parse_getprop(output):
        values = {}
        for line in (output or "").splitlines():
            match = re.match(r"^\[([^]]+)\]: \[(.*)\]$", line.strip())
            if match:
                values[match.group(1)] = match.group(2)
        return values

    def _read_ai_pq_state(self, serial):
        result = legacy.adb_command(
            "shell", "getprop", serial=serial, timeout=30, telemetry=False)
        return result, self._parse_getprop(result[1])

    def _ensure_adb_root(self, serial):
        identity = legacy.adb_command(
            "shell", "id", serial=serial, timeout=15, telemetry=False)
        if identity[0] == 0 and "uid=0(root)" in identity[1]:
            return True
        result = legacy.adb_command("root", serial=serial, timeout=30)
        if result[0] != 0 or "cannot run as root" in result[1].lower():
            return False
        time.sleep(2.0)
        legacy.run_command([legacy.ADB, "connect", serial], timeout=20)
        identity = legacy.adb_command(
            "shell", "id", serial=serial, timeout=15, telemetry=False)
        return identity[0] == 0 and "uid=0(root)" in identity[1]

    def _device_changed(self, serial):
        serial = serial.strip()
        if not serial:
            self._set_ai_pq_status("Waiting for a device", "idle")
            self._ai_guard_last_serial = None
            return
        if serial != self._ai_guard_last_serial:
            self._ai_guard_last_serial = serial
            self._set_ai_pq_status("Checking hardware picture processing…", "pending")
            self.enforce_ai_pq_off(silent=True)

    def _ai_pq_watchdog(self):
        if self.active_serial():
            self.enforce_ai_pq_off(silent=True)

    def verify_ai_pq_state(self):
        serial = self.require_serial()
        if not serial:
            return
        self._set_ai_pq_status("Verifying device properties…", "pending")

        def job():
            result, values = self._read_ai_pq_state(serial)
            if result[0] != 0:
                self.ui(self._set_ai_pq_status, "Unable to read AI-PQ properties", "error")
                return
            drift = {
                name: values.get(name, "<missing>")
                for name, expected in AI_PQ_PROPERTIES.items()
                if values.get(name) != expected
            }
            if drift:
                detail = ", ".join(f"{name}={value}" for name, value in drift.items())
                self.logline("[AI-PQ] Enabled or inconsistent properties: " + detail)
                self.ui(
                    self._set_ai_pq_status,
                    f"Attention · {len(drift)} setting(s) are not off", "error")
            else:
                self.ui(
                    self._set_ai_pq_status,
                    f"Protected · {len(AI_PQ_PROPERTIES)}/{len(AI_PQ_PROPERTIES)} verified off",
                    "online")
        self.worker(job)

    def enforce_ai_pq_off(self, silent=False):
        serial = self.active_serial()
        if not serial:
            if not silent:
                self.require_serial()
            return
        if not self._ai_guard_lock.acquire(blocking=False):
            return
        if not silent:
            self._set_ai_pq_status("Enforcing hardware off-state…", "pending")

        def job():
            try:
                result, values = self._read_ai_pq_state(serial)
                if result[0] != 0:
                    self.ui(self._set_ai_pq_status, "Device property read failed", "error")
                    return
                drift = {
                    name: values.get(name, "<missing>")
                    for name, expected in AI_PQ_PROPERTIES.items()
                    if values.get(name) != expected
                }
                if not drift:
                    self.ui(
                        self._set_ai_pq_status,
                        f"Protected · {len(AI_PQ_PROPERTIES)}/{len(AI_PQ_PROPERTIES)} verified off",
                        "online")
                    return

                self.ui(
                    self._set_ai_pq_status,
                    f"Correcting {len(drift)} hardware setting(s)…", "pending")
                if not self._ensure_adb_root(serial):
                    self.logline("[AI-PQ] ADB root is required but could not be enabled.")
                    self.ui(self._set_ai_pq_status, "Root access unavailable", "error")
                    return

                legacy.adb_command(
                    "shell", "stop", "rkswpq_demo", serial=serial, timeout=20)
                failures = []
                for name, expected in AI_PQ_PROPERTIES.items():
                    set_result = legacy.adb_command(
                        "shell", "setprop", name, expected,
                        serial=serial, timeout=20)
                    if set_result[0] != 0:
                        failures.append(name)

                # Force the vendor display pipeline to consume the new values,
                # even when its UI toggle reports stale state.
                legacy.adb_command(
                    "shell", "setprop", "vendor.tvinput.rkpq.update_vdpp_cfg", "0",
                    serial=serial, timeout=20)
                legacy.adb_command(
                    "shell", "setprop", "vendor.tvinput.rkpq.update_vdpp_cfg", "1",
                    serial=serial, timeout=20)
                time.sleep(0.8)

                verify_result, verified = self._read_ai_pq_state(serial)
                remaining = {
                    name: verified.get(name, "<missing>")
                    for name, expected in AI_PQ_PROPERTIES.items()
                    if verified.get(name) != expected
                }
                if verify_result[0] != 0 or failures or remaining:
                    detail = ", ".join(
                        f"{name}={value}" for name, value in remaining.items())
                    self.logline(
                        "[AI-PQ] Verification failed after enforcement: " +
                        (detail or ", ".join(failures) or "device read failed"))
                    self.ui(
                        self._set_ai_pq_status,
                        f"Enforcement incomplete · {len(remaining) or len(failures)} remaining",
                        "error")
                    return

                self.logline(
                    f"[AI-PQ] Corrected {len(drift)} setting(s); hardware state verified off.")
                self.ui(
                    self._set_ai_pq_status,
                    f"Protected · corrected {len(drift)}, all verified off", "online")
            finally:
                self._ai_guard_lock.release()
        self.worker(job)

    def open_display_settings(self):
        serial = self.require_serial()
        if not serial:
            return
        self.status_var.set("Opening display settings on the device…")

        def job():
            args = ["shell", "am", "start", "-a", "android.settings.DISPLAY_SETTINGS"]
            result = legacy.adb_command(*args, serial=serial, timeout=30)
            self.log_adb_result("Display Settings", serial, args, result)
            self.set_status(
                "Display settings opened" if result[0] == 0
                else "Could not open display settings")
        self.worker(job)

    # ---------- compatibility + telemetry ----------
    def _execute_ui(self, callback, args, kwargs):
        try:
            callback(*tuple(args), **dict(kwargs))
        except Exception as exc:
            self.console.append_message(f"[UI ERROR] {exc}", RED)

    def ui(self, callback, *args, **kwargs):
        self._bridge.requested.emit(callback, args, kwargs)

    def worker(self, callback):
        threading.Thread(target=callback, daemon=True).start()

    def after(self, milliseconds, callback, *args):
        self._timer_sequence += 1
        timer_id = self._timer_sequence
        timer = QTimer(self)
        timer.setSingleShot(True)

        def fire():
            self._timers.pop(timer_id, None)
            callback(*args)
            timer.deleteLater()

        timer.timeout.connect(fire)
        self._timers[timer_id] = timer
        timer.start(int(milliseconds))
        return timer_id

    def after_cancel(self, timer_id):
        timer = self._timers.pop(timer_id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def logline(self, text):
        color = "#B9C8DD"
        upper = text.upper()
        if "FAILED" in upper or "ERROR" in upper or "NOT FOUND" in upper:
            color = RED
        elif text.startswith("[SSH]"):
            color = "#79D8FF"
        elif text.startswith("[EXEC]"):
            color = BLUE
        elif "SAVED" in upper or "CONNECTED" in upper or "READY" in upper:
            color = GREEN
        self.ui(self.console.append_message, text, color)

    def _observe_command(self, event, command, result=None):
        if event == "start":
            self.logline("[EXEC] " + legacy.command_text(command))
            return
        rc, output, elapsed = result
        summary = f"[EXIT {rc}] {elapsed:.2f}s"
        if output:
            summary += "\n" + output
        self.logline(summary)

    def _set_tunnel_state(self, text, tone="idle"):
        self.tunnel_label.setText(text)
        self.tunnel_label.setProperty("tone", tone)
        self.tunnel_label.style().unpolish(self.tunnel_label)
        self.tunnel_label.style().polish(self.tunnel_label)

    # ---------- Qt-native scrcpy embedding ----------
    def _adopt_scrcpy_window(self, hwnd):
        if not (self.scrcpy_proc and self.scrcpy_proc.poll() is None):
            return
        try:
            self.scrcpy_hwnd = hwnd
            self.mirror_frame.adopt_window(hwnd)
            self.logline("[Mirror] scrcpy embedded into the Qt workspace.")
        except Exception as exc:
            self.scrcpy_hwnd = None
            self.logline(f"[Mirror] Could not embed scrcpy ({exc}); it remains separate.")

    def _resize_embedded_mirror(self):
        # QWidget.createWindowContainer keeps the foreign window fitted to the
        # available frame without the clipping caused by manual pixel sizing.
        return

    def _clear_mirror_embed(self):
        self.scrcpy_hwnd = None
        self.mirror_aspect = None
        self.mirror_frame.clear_window()

    def run_claude(self):
        serial = self.active_serial()
        environment = self._terminal_environment(serial)
        working_directory = legacy.app_dir()
        if getattr(sys, "frozen", False) and os.path.basename(working_directory).lower() == "dist":
            working_directory = os.path.dirname(working_directory)
        try:
            if os.name == "nt":
                wt = shutil.which("wt.exe", path=environment.get("PATH"))
                command = ([wt, "-d", working_directory, "powershell.exe", "-NoExit",
                            "-Command", "claude"] if wt else
                           ["powershell.exe", "-NoExit", "-Command", "claude"])
                self.logline("[TERMINAL] " + legacy.command_text(command))
                subprocess.Popen(command, cwd=working_directory, env=environment,
                                 creationflags=legacy._NEW_CONSOLE)
            elif sys.platform == "darwin":
                command = ["osascript", "-e",
                           f'tell application "Terminal" to do script "cd {working_directory} && claude"']
                self.logline("[TERMINAL] " + legacy.command_text(command))
                subprocess.Popen(command, env=environment)
            else:
                command = ["x-terminal-emulator", "-e", "claude"]
                self.logline("[TERMINAL] " + legacy.command_text(command))
                subprocess.Popen(command, cwd=working_directory, env=environment)
            self.status_var.set("Claude terminal opened" + (f" for {serial}" if serial else ""))
        except Exception as exc:
            QtMessageBoxAdapter.showerror(APP_NAME, f"Could not open Claude:\n{exc}")

    def closeEvent(self, event):
        if self.capture_proc:
            if QtMessageBoxAdapter.askyesno(
                    APP_NAME, "A CDC diagnostic session is recording.\n\nStop and save it now?"):
                self.stop_debug_capture()
                QtMessageBoxAdapter.showinfo(
                    APP_NAME,
                    "The diagnostic ZIP is being saved. Close CDC again when saving finishes.")
            event.ignore()
            return
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            try:
                self.scrcpy_proc.terminate()
            except Exception:
                pass
        if self.ssh_proc and self.ssh_proc.poll() is None:
            try:
                self.ssh_proc.terminate()
            except Exception:
                pass
        legacy.set_command_observer(None)
        event.accept()


def main():
    _startup_trace("QApplication construction started")
    app = QApplication(sys.argv)
    _startup_trace(f"QApplication constructed (platform={app.platformName()})")
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Convrse")
    app.setWindowIcon(QIcon(resource_path("assets/convrse-logo.png")))
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    _startup_trace("Application theme applied")
    window = CdcMainWindow()
    window.showNormal()
    window.raise_()
    window.activateWindow()
    native_id = int(window.winId())
    geometry = window.geometry()
    _startup_trace(
        "Main window shown; event loop starting "
        f"(visible={window.isVisible()}, hwnd={native_id}, "
        f"geometry={geometry.width()}x{geometry.height()})")
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException:
        _startup_trace("FATAL\n" + traceback.format_exc())
        raise
