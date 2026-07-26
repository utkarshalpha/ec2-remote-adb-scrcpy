#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convrse Device Control: remote, mirror, recovery, and diagnostics."""

import csv
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    # The Qt build deliberately ships without Tcl/Tk.  Keep this module
    # importable so its device-operation methods can be shared by cdc_qt.py;
    # the legacy Tk window is simply unavailable in that packaged runtime.
    class _TkUnavailable:
        class Tk:
            pass

        class TclError(Exception):
            pass

    tk = _TkUnavailable()
    filedialog = None
    messagebox = None
    ttk = None


APP_NAME = "Convrse Device Control"
APP_SHORT_NAME = "CDC"
DEFAULT_IP = "127.0.0.1"
DEFAULT_ADB_PORT = "17000"
CONVRSE_STORE_URL = "https://tinyurl.com/convrseapp"
SSH_HOST = "ubuntu@cdm.convrse.ai"
CLEANUP_PACKAGE = "com.charon.rocketfly"
SESSION_SECONDS = 30 * 60

SESSION_SAMPLE_SECONDS = 30  # metric cadence; keeps the SSH tunnel load sane

# CDC visual system: a focused, high-contrast operations console.
BG = "#080D18"
BG_ALT = "#0C1322"
SIDEBAR = "#0E1728"
SURFACE = "#152033"
SURFACE_HOVER = "#1D2B42"
OVERLAY = "#263650"
BORDER = "#22314A"
TEXT = "#F4F7FB"
TEXT_MUTED = "#93A4BF"
TEXT_SUBTLE = "#6F819D"
BLUE = "#66A3FF"
BLUE_HOVER = "#82B4FF"
GREEN = "#43D6A3"
AMBER = "#F4C95D"
RED = "#FF7081"
RED_DARK = "#472331"
MIRROR_BG = "#03060B"
WHITE = "#FFFFFF"

FONT = "Segoe UI"
FONT_MONO = "Cascadia Mono"

_NO_WINDOW = 0
_NEW_CONSOLE = 0
if os.name == "nt":
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

# Win32 pieces used to re-parent the scrcpy window into the CDC window.
_USER32 = None
if os.name == "nt":
    import ctypes
    _USER32 = ctypes.windll.user32
_GWL_STYLE = -16
_WS_CHILD = 0x40000000
_WS_VISIBLE = 0x10000000


STREAM_PROFILES = {
    "Ultra Low  •  360p / 500K": ("640", "500K"),
    "Low  •  480p / 1M": ("854", "1M"),
    "Balanced  •  720p / 2M": ("1280", "2M"),
    "Clear  •  720p / 5M": ("1280", "5M"),
}
DEFAULT_PROFILE = "Low  •  480p / 1M"

PROTECTED_PACKAGES = {
    "android",
    "com.android.settings",
    "com.android.tv.settings",
    "com.android.providers.settings",
    "com.android.systemui",
    "com.google.android.permissioncontroller",
}

def app_dir():
    """Directory containing the script or frozen executable."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    candidates = []

    # PyInstaller one-file builds extract bundled files under ``sys._MEIPASS``.
    # Keep the complete scrcpy distribution in one subdirectory so scrcpy.exe,
    # scrcpy-server, ADB, and their DLLs remain beside one another at runtime.
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        candidates.extend((
            os.path.join(bundle_root, "scrcpy-runtime", exe),
            # Preserve compatibility with older packages that placed tools at
            # the root of the PyInstaller extraction directory.
            os.path.join(bundle_root, exe),
        ))

    executable_root = app_dir()
    candidates.extend((
        # A sidecar runtime is useful for development and emergency upgrades.
        os.path.join(executable_root, "scrcpy-runtime", exe),
        os.path.join(executable_root, exe),
    ))

    seen = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isfile(candidate):
            return candidate
    return name


ADB = find_tool("adb")
SCRCPY = find_tool("scrcpy")

_COMMAND_OBSERVER = None


def set_command_observer(callback):
    """Register an optional command telemetry callback for desktop frontends."""
    global _COMMAND_OBSERVER
    _COMMAND_OBSERVER = callback


def _notify_command_observer(event, command, result=None):
    callback = _COMMAND_OBSERVER
    if callback is None:
        return
    try:
        callback(event, list(command), result)
    except Exception:
        # Telemetry must never break a device operation.
        pass


def run_command(command, timeout=60, telemetry=True):
    """Return (return_code, combined_output, elapsed_seconds)."""
    started = time.monotonic()
    if telemetry:
        _notify_command_observer("start", command)
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        result = (proc.returncode, output, time.monotonic() - started)
    except FileNotFoundError:
        output = f"Not found: {command[0]} (place adb/scrcpy beside this app or on PATH)"
        result = (127, output, time.monotonic() - started)
    except subprocess.TimeoutExpired:
        result = (124, "Command timed out", time.monotonic() - started)
    except Exception as exc:  # GUI commands must report errors instead of crashing.
        result = (1, str(exc), time.monotonic() - started)
    if telemetry:
        _notify_command_observer("finish", command, result)
    return result


def adb_command(*args, serial=None, timeout=60, telemetry=True):
    command = [ADB]
    if serial:
        command += ["-s", serial]
    command += [str(arg) for arg in args]
    return run_command(command, timeout=timeout, telemetry=telemetry)


def command_text(command):
    if os.name == "nt":
        return subprocess.list2cmdline([str(part) for part in command])
    return " ".join(shlex.quote(str(part)) for part in command)


def validated_port(value):
    text = str(value).strip()
    if not text.isdigit():
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


def build_ssh_command(ssh_executable, pem_path, port):
    """Build the managed local ADB tunnel without invoking a shell."""
    return [
        ssh_executable,
        "-i", pem_path,
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-N",
        "-L", f"{port}:localhost:{port}",
        SSH_HOST,
    ]


def parse_component_package(text):
    """Extract a package from a package/activity component in dumpsys output."""
    patterns = (
        r"(?:mCurrentFocus|mFocusedApp|topResumedActivity|mResumedActivity|ResumedActivity)[^\n]*?\bu\d+\s+([A-Za-z0-9_.$]+)/(?:[A-Za-z0-9_.$]+)",
        r"(?:mCurrentFocus|mFocusedApp|topResumedActivity|mResumedActivity|ResumedActivity)[^\n]*?\b([A-Za-z0-9_.$]+)/(?:[A-Za-z0-9_.$]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", re.IGNORECASE)
        if match:
            return match.group(1)
    return None


class ScrcpyRemote(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.configure(bg=BG)
        self.geometry("1420x920")
        self.minsize(1220, 840)

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
        self.session_status_var = tk.StringVar(master=self, value="Diagnostics idle")
        self.session_detail_var = tk.StringVar(master=self, value="")
        self.scrcpy_hwnd = None
        self.mirror_aspect = None
        self.session_touch_downs = 0
        self.session_tracking_downs = 0
        self.session_clicks = 0
        self.session_keys = 0

        self._build_style()
        self._build_dashboard_ui()
        self.bind("<F5>", lambda _event: self.refresh_devices())
        self.bind("<Control-l>", lambda _event: self.toggle_log())
        self.after(20, self._enable_dark_titlebar)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self.refresh_devices)

    # ---------- UI ----------
    def _enable_dark_titlebar(self):
        """Ask Windows to render native window chrome in the CDC dark theme."""
        if os.name != "nt":
            return
        try:
            value = ctypes.c_int(1)
            hwnd = self.winfo_id()
            # 20 is current Windows 10/11; 19 covers older Windows 10 builds.
            for attribute in (20, 19):
                result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
                if result == 0:
                    break
        except Exception:
            pass

    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=(FONT, 9))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 9))

        style.configure("Header.TFrame", background=BG_ALT)
        style.configure("BrandMark.TLabel", background=BLUE, foreground=BG,
                        font=(FONT, 11, "bold"), padding=(10, 7))
        style.configure("Title.TLabel", background=BG_ALT, foreground=TEXT,
                        font=(FONT, 18, "bold"))
        style.configure("Subtitle.TLabel", background=BG_ALT, foreground=TEXT_MUTED,
                        font=(FONT, 8, "bold"))
        style.configure("HeaderMeta.TLabel", background=SURFACE, foreground=TEXT_SUBTLE,
                        font=(FONT, 7, "bold"))
        style.configure("HeaderStatus.TFrame", background=SURFACE)
        style.configure("TunnelIdle.TLabel", background=SURFACE, foreground=TEXT_MUTED,
                        font=(FONT, 9, "bold"))
        style.configure("TunnelPending.TLabel", background=SURFACE, foreground=AMBER,
                        font=(FONT, 9, "bold"))
        style.configure("TunnelOnline.TLabel", background=SURFACE, foreground=GREEN,
                        font=(FONT, 9, "bold"))
        style.configure("TunnelError.TLabel", background=SURFACE, foreground=RED,
                        font=(FONT, 9, "bold"))

        style.configure("Sidebar.TFrame", background=SIDEBAR)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("CardTitle.TLabel", background=SURFACE, foreground=TEXT,
                        font=(FONT, 10, "bold"))
        style.configure("CardMeta.TLabel", background=SURFACE, foreground=TEXT_SUBTLE,
                        font=(FONT, 7, "bold"))
        style.configure("CardBody.TLabel", background=SURFACE, foreground=TEXT_MUTED,
                        font=(FONT, 8))
        style.configure("Package.TLabel", background=BG_ALT, foreground=TEXT_MUTED,
                        font=(FONT_MONO, 8), padding=(8, 6))

        style.configure("Stage.TFrame", background=BG_ALT)
        style.configure("StageToolbar.TFrame", background=SURFACE)
        style.configure("StageTitle.TLabel", background=SURFACE, foreground=TEXT,
                        font=(FONT, 12, "bold"))
        style.configure("StageMeta.TLabel", background=SURFACE, foreground=TEXT_MUTED,
                        font=(FONT, 8))
        style.configure("StageOverline.TLabel", background=SURFACE, foreground=BLUE,
                        font=(FONT, 7, "bold"))

        style.configure("TButton", background=SURFACE_HOVER, foreground=TEXT,
                        bordercolor=SURFACE_HOVER, borderwidth=1, relief="flat",
                        focusthickness=0, focuscolor=SURFACE_HOVER,
                        font=(FONT, 8), padding=(8, 6))
        style.map("TButton",
                  background=[("disabled", SURFACE), ("pressed", OVERLAY),
                              ("active", OVERLAY)],
                  foreground=[("disabled", TEXT_SUBTLE), ("active", WHITE)],
                  bordercolor=[("active", OVERLAY)])
        style.configure("Accent.TButton", background=BLUE, foreground=BG,
                        bordercolor=BLUE, font=(FONT, 8, "bold"))
        style.map("Accent.TButton",
                  background=[("disabled", OVERLAY), ("pressed", BLUE_HOVER),
                              ("active", BLUE_HOVER)],
                  foreground=[("disabled", TEXT_SUBTLE), ("active", BG)])
        style.configure("Danger.TButton", background=RED_DARK, foreground="#FFB6BF",
                        bordercolor="#653044")
        style.map("Danger.TButton", background=[("active", RED)],
                  foreground=[("active", BG)], bordercolor=[("active", RED)])
        style.configure("Quiet.TButton", background=SURFACE, foreground=TEXT_MUTED,
                        bordercolor=BORDER)
        style.map("Quiet.TButton", background=[("active", SURFACE_HOVER)],
                  foreground=[("active", TEXT)])

        style.configure("TEntry", fieldbackground=BG_ALT, foreground=TEXT,
                        insertcolor=TEXT, bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, borderwidth=1, padding=(8, 6))
        style.map("TEntry", bordercolor=[("focus", BLUE)],
                  lightcolor=[("focus", BLUE)], darkcolor=[("focus", BLUE)])
        style.configure("TCombobox", fieldbackground=BG_ALT, foreground=TEXT,
                        background=BG_ALT, arrowcolor=TEXT_MUTED,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        borderwidth=1, padding=(8, 5))
        style.map("TCombobox",
                  fieldbackground=[("readonly", BG_ALT)],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", BG_ALT)],
                  selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", BLUE)])

        style.configure("Footer.TFrame", background=BG_ALT)
        style.configure("Footer.TLabel", background=BG_ALT, foreground=TEXT_MUTED,
                        font=(FONT, 8))
        style.configure("FooterDot.TLabel", background=BG_ALT, foreground=GREEN,
                        font=(FONT, 8))
        style.configure("FooterHint.TLabel", background=BG_ALT, foreground=TEXT_SUBTLE,
                        font=(FONT, 7))

    def _build_dashboard_ui(self):
        self.content = ttk.Frame(self)
        self.content.pack(fill="both", expand=True)

        header = ttk.Frame(self.content, style="Header.TFrame", padding=(18, 13))
        header.pack(fill="x")
        ttk.Label(header, text="CDC", style="BrandMark.TLabel").pack(
            side="left", padx=(0, 12))
        title_group = ttk.Frame(header, style="Header.TFrame")
        title_group.pack(side="left")
        ttk.Label(title_group, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_group, text="REMOTE OPERATIONS  /  MIRROR  /  DIAGNOSTICS",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(1, 0))

        tunnel_panel = ttk.Frame(header, style="HeaderStatus.TFrame", padding=(12, 8))
        tunnel_panel.pack(side="right")
        ttk.Label(tunnel_panel, text="SECURE TUNNEL", style="HeaderMeta.TLabel").pack(
            anchor="w")
        self.tunnel_var = tk.StringVar(value="Disconnected")
        self.tunnel_status_label = ttk.Label(
            tunnel_panel, textvariable=self.tunnel_var, style="TunnelIdle.TLabel")
        self.tunnel_status_label.pack(anchor="w")

        main = ttk.Frame(self.content, padding=(14, 14, 14, 10))
        main.pack(fill="both", expand=True)
        sidebar = ttk.Frame(main, style="Sidebar.TFrame", padding=(10, 8))
        stage = ttk.Frame(main, style="Stage.TFrame")
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        stage.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=0, minsize=350)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        self._section_connection(sidebar)
        self._section_remote(sidebar)
        self._section_app_recovery(sidebar)
        self._section_utilities(sidebar)
        self._section_stream(stage)
        self._section_status()

    def _sidebar_group(self, parent, title):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(10, 8))
        card.pack(fill="x", pady=(0, 7))
        ttk.Label(card, text=title, style="CardTitle.TLabel").pack(
            anchor="w", pady=(0, 7))
        group = ttk.Frame(card, style="Card.TFrame")
        group.pack(fill="x")
        return group

    def _section_connection(self, parent=None):
        group = self._sidebar_group(parent, "Device connection")
        self.pem_var = tk.StringVar()
        self.port_var = tk.StringVar(value=DEFAULT_ADB_PORT)
        self.ip_var = tk.StringVar(value=DEFAULT_IP)
        self.device_var = tk.StringVar()

        ttk.Label(group, text="SSH PRIVATE KEY", style="CardMeta.TLabel").grid(
            row=0, column=0, columnspan=5, sticky="w", pady=(0, 3))
        ttk.Entry(group, textvariable=self.pem_var).grid(
            row=1, column=0, columnspan=4, sticky="ew")
        ttk.Button(group, text="Browse", style="Quiet.TButton",
                   command=self.select_pem_key).grid(
            row=1, column=4, sticky="ew", padx=(5, 0))

        ttk.Label(group, text="ADB PORT", style="CardMeta.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(7, 3))
        ttk.Label(group, text="LOCAL HOST", style="CardMeta.TLabel").grid(
            row=2, column=2, columnspan=3, sticky="w", padx=(7, 0), pady=(7, 3))
        ttk.Entry(group, textvariable=self.port_var, width=7).grid(
            row=3, column=0, columnspan=2, sticky="ew")
        ttk.Entry(group, textvariable=self.ip_var, width=12).grid(
            row=3, column=2, columnspan=3, sticky="ew", padx=(7, 0))

        self.tunnel_btn = ttk.Button(group, text="Connect device",
                                     style="Accent.TButton", command=self.connect_tunnel)
        self.tunnel_btn.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(group, text="Disconnect", style="Quiet.TButton",
                   command=self.disconnect_tunnel).grid(
            row=4, column=3, columnspan=2, sticky="ew", padx=(5, 0), pady=(8, 0))

        ttk.Label(group, text="ACTIVE DEVICE", style="CardMeta.TLabel").grid(
            row=5, column=0, columnspan=5, sticky="w", pady=(8, 3))
        self.device_box = ttk.Combobox(group, textvariable=self.device_var,
                                       state="readonly")
        self.device_box.grid(row=6, column=0, columnspan=4, sticky="ew")
        ttk.Button(group, text="Refresh", style="Quiet.TButton",
                   command=self.refresh_devices).grid(
            row=6, column=4, sticky="ew", padx=(5, 0))
        ttk.Label(group, text=f"Gateway  {SSH_HOST}", style="CardBody.TLabel").grid(
            row=7, column=0, columnspan=5, sticky="w", pady=(6, 0))
        for column in range(5):
            group.columnconfigure(column, weight=1)

    def _section_remote(self, parent):
        group = self._sidebar_group(parent, "Remote control")
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
        for index, (label, command) in enumerate(buttons):
            ttk.Button(group, text=label, command=command).grid(
                row=index // 3, column=index % 3, sticky="nsew", padx=2, pady=2)
        for column in range(3):
            group.columnconfigure(column, weight=1, uniform="remote")
        self.key_var = tk.StringVar()
        ttk.Entry(group, textvariable=self.key_var, width=8).grid(
            row=5, column=0, sticky="ew", padx=2, pady=(4, 0))
        ttk.Button(group, text="Send key code", command=self.send_custom_keyevent).grid(
            row=5, column=1, columnspan=2, sticky="ew", padx=2, pady=(4, 0))

    def _section_app_recovery(self, parent):
        group = self._sidebar_group(parent, "App recovery")
        self.current_app_var = tk.StringVar(value="Foreground app: detected automatically")
        ttk.Label(group, textvariable=self.current_app_var, style="Package.TLabel",
                  wraplength=320, justify="left").grid(
            row=0, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        controls = [
            ("Force Stop", self.force_stop_current, None),
            ("Restart App", self.restart_current, "Accent.TButton"),
            ("Clear Cache", self.clear_current_cache, None),
            ("Clear Data", self.clear_current_data, "Danger.TButton"),
            ("Close Background", self.close_background_apps, None),
            ("Export Logs", self.save_recent_logs, None),
        ]
        for index, (label, command, style_name) in enumerate(controls):
            options = {"text": label, "command": command}
            if style_name:
                options["style"] = style_name
            ttk.Button(group, **options).grid(
                row=1 + index // 3, column=index % 3, sticky="nsew", padx=2, pady=2)
        for column in range(3):
            group.columnconfigure(column, weight=1, uniform="recovery")

    def _section_utilities(self, parent):
        group = self._sidebar_group(parent, "Tools & diagnostics")
        controls = [
            ("Install APK", self.install_apk),
            ("Open Store", self.open_convrse_store),
            ("Open CleanUp", self.open_cleanup_app),
            ("Launch Claude", self.run_claude),
        ]
        for index, (label, command) in enumerate(controls):
            ttk.Button(group, text=label, command=command).grid(
                row=index // 2, column=index % 2, sticky="nsew", padx=2, pady=2)
        self.capture_btn = ttk.Button(group, text="Start diagnostic session",
                                      style="Accent.TButton",
                                      command=self.toggle_debug_capture)
        self.capture_btn.grid(row=2, column=0, columnspan=2, sticky="ew", padx=2, pady=2)
        group.columnconfigure(0, weight=1, uniform="tools")
        group.columnconfigure(1, weight=1, uniform="tools")

    def _section_stream(self, parent):
        toolbar = ttk.Frame(parent, style="StageToolbar.TFrame", padding=(14, 11))
        toolbar.pack(fill="x")
        heading = ttk.Frame(toolbar, style="StageToolbar.TFrame")
        heading.pack(side="left")
        ttk.Label(heading, text="LIVE DEVICE", style="StageOverline.TLabel").pack(anchor="w")
        ttk.Label(heading, text="Mirror workspace", style="StageTitle.TLabel").pack(anchor="w")
        ttk.Label(heading, text="View and operate the selected Android endpoint",
                  style="StageMeta.TLabel").pack(anchor="w")

        controls = ttk.Frame(toolbar, style="StageToolbar.TFrame")
        controls.pack(side="right")
        ttk.Label(controls, textvariable=self.session_status_var,
                  style="StageMeta.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="e", pady=(0, 4))
        self.profile_var = tk.StringVar(value=DEFAULT_PROFILE)
        ttk.Combobox(controls, textvariable=self.profile_var, state="readonly",
                     values=list(STREAM_PROFILES), width=24).grid(row=1, column=0, sticky="e")
        self.start_btn = ttk.Button(controls, text="Start mirror", style="Accent.TButton",
                                    command=self.start_scrcpy)
        self.start_btn.grid(row=1, column=1, padx=(7, 4))
        ttk.Button(controls, text="Stop", style="Quiet.TButton",
                   command=self.stop_scrcpy).grid(row=1, column=2)
        ttk.Label(controls, textvariable=self.session_detail_var,
                  style="StageMeta.TLabel", wraplength=460, justify="right").grid(
            row=2, column=0, columnspan=3, sticky="e", pady=(5, 0))

        # The scrcpy window is re-parented into this frame, so the mirror and the
        # remote live together in one CDC window.
        self.mirror_frame = tk.Frame(parent, bg=MIRROR_BG, highlightthickness=1,
                                     highlightbackground=BORDER)
        self.mirror_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.mirror_frame.bind("<Configure>", lambda _e: self._resize_embedded_mirror())
        self.mirror_hint = tk.Frame(self.mirror_frame, bg=MIRROR_BG)
        tk.Label(self.mirror_hint, text="NO ACTIVE MIRROR", bg=MIRROR_BG, fg=TEXT_SUBTLE,
                 font=(FONT, 8, "bold")).pack()
        tk.Label(self.mirror_hint, text="Your device will appear here", bg=MIRROR_BG,
                 fg=TEXT, font=(FONT, 16, "bold")).pack(pady=(7, 3))
        tk.Label(self.mirror_hint,
                 text="Connect a device, choose a stream profile, then start the mirror.",
                 bg=MIRROR_BG, fg=TEXT_MUTED, font=(FONT, 9)).pack()
        self.mirror_hint.place(relx=0.5, rely=0.5, anchor="center")

    def _section_status(self):
        self.status_var = tk.StringVar(value="Ready")
        self.status_bar = ttk.Frame(
            self.content, style="Footer.TFrame", padding=(16, 8))
        self.status_bar.pack(fill="x")
        ttk.Label(self.status_bar, text="●", style="FooterDot.TLabel").pack(
            side="left", padx=(0, 7))
        ttk.Label(self.status_bar, textvariable=self.status_var,
                  style="Footer.TLabel").pack(side="left")
        self.log_visible = False
        self.log_btn = ttk.Button(self.status_bar, text="Activity log",
                                  style="Quiet.TButton", command=self.toggle_log)
        self.log_btn.pack(side="right")
        ttk.Label(self.status_bar, text="F5  Refresh device   ·   Ctrl+L  Toggle log",
                  style="FooterHint.TLabel").pack(side="right", padx=(0, 12))
        self.log = tk.Text(self.content, height=7, bg=BG_ALT, fg=TEXT_MUTED, bd=0,
                           highlightthickness=1, highlightbackground=BORDER,
                           insertbackground=TEXT, selectbackground=OVERLAY,
                           font=(FONT_MONO, 8), wrap="word", padx=10, pady=8)
        self.log.configure(state="disabled")

    def toggle_log(self):
        if self.log_visible:
            self.log.pack_forget()
            self.log_btn.configure(text="Activity log")
        else:
            self.log.pack(fill="x", padx=14, pady=(0, 10), before=self.status_bar)
            self.log_btn.configure(text="Close log")
        self.log_visible = not self.log_visible

    def _set_tunnel_state(self, text, tone="idle"):
        styles = {
            "idle": "TunnelIdle.TLabel",
            "pending": "TunnelPending.TLabel",
            "online": "TunnelOnline.TLabel",
            "error": "TunnelError.TLabel",
        }
        self.tunnel_var.set(text)
        self.tunnel_status_label.configure(style=styles.get(tone, styles["idle"]))

    # ---------- thread-safe helpers ----------
    def ui(self, callback, *args, **kwargs):
        try:
            if kwargs:
                self.after(0, lambda: callback(*args, **kwargs))
            else:
                self.after(0, callback, *args)
        except tk.TclError:
            pass

    def worker(self, callback):
        threading.Thread(target=callback, daemon=True).start()

    def set_status(self, text):
        self.ui(self.status_var.set, text)

    def logline(self, text):
        def append():
            self.log.configure(state="normal")
            self.log.insert("end", text.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.ui(append)

    def active_serial(self):
        value = self.device_var.get().strip()
        return value or None

    def require_serial(self):
        serial = self.active_serial()
        if not serial:
            messagebox.showwarning(APP_NAME, "No active device. Connect or refresh devices first.")
        return serial

    def log_adb_result(self, action, serial, args, result):
        rc, output, elapsed = result
        full = [ADB, "-s", serial, *args]
        self.logline(
            f"[{action}]\nCommand: {command_text(full)}\n"
            f"Result: rc={rc}, {elapsed:.2f}s\n{output or '(no output)'}"
        )

    # ---------- SSH tunnel / connection ----------
    def select_pem_key(self):
        path = filedialog.askopenfilename(
            title="Select SSH PEM private key",
            filetypes=[("PEM private key", "*.pem"), ("All files", "*.*")],
        )
        if path:
            self.pem_var.set(path)

    @staticmethod
    def _local_port_open(port):
        try:
            with socket.create_connection((DEFAULT_IP, port), timeout=0.5):
                return True
        except OSError:
            return False

    def connect_tunnel(self):
        pem_path = os.path.abspath(os.path.expanduser(self.pem_var.get().strip().strip('"')))
        port = validated_port(self.port_var.get())
        if not self.pem_var.get().strip() or not os.path.isfile(pem_path):
            messagebox.showwarning(APP_NAME, "Select a valid PEM private-key file first.")
            return
        if port is None:
            messagebox.showwarning(APP_NAME, "Enter a valid ADB port from 1 to 65535.")
            return
        ssh_executable = shutil.which("ssh.exe") or shutil.which("ssh")
        if not ssh_executable:
            messagebox.showerror(
                APP_NAME,
                "Windows OpenSSH was not found. Install the OpenSSH Client optional feature first.",
            )
            return

        self.ip_var.set(DEFAULT_IP)
        self.port_var.set(str(port))
        target = f"{DEFAULT_IP}:{port}"
        if self.ssh_proc and self.ssh_proc.poll() is None:
            if self.ssh_port == port and self.ssh_pem == pem_path:
                self.status_var.set("Tunnel already running; connecting ADB …")
                self.worker(lambda: self._connect_adb_target(target))
                return
            try:
                self.ssh_proc.terminate()
                self.ssh_proc.wait(timeout=5)
            except Exception:
                try:
                    self.ssh_proc.kill()
                except Exception:
                    pass
            self.ssh_proc = None

        self.status_var.set(f"Starting SSH tunnel on port {port} …")
        self._set_tunnel_state("Connecting…", "pending")
        self.tunnel_btn.configure(state="disabled")

        def job():
            # If a terminal already owns this forwarding port, use that tunnel.
            if self._local_port_open(port):
                self.logline(f"[SSH] Local port {port} is already active; using the existing listener.")
                self.ui(self._set_tunnel_state, f"Active on {port}", "online")
                self._connect_adb_target(target)
                self.ui(self.tunnel_btn.configure, state="normal")
                return

            command = build_ssh_command(ssh_executable, pem_path, port)
            self.logline("[SSH] " + command_text(command))
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    creationflags=_NO_WINDOW,
                )
            except Exception as exc:
                self.set_status("Could not start the SSH tunnel")
                self.logline("[SSH] " + str(exc))
                self.ui(self._set_tunnel_state, "Connection failed", "error")
                self.ui(self.tunnel_btn.configure, state="normal")
                return

            self.ssh_proc = proc
            self.ssh_port = port
            self.ssh_pem = pem_path
            threading.Thread(target=self._watch_ssh_output, args=(proc,), daemon=True).start()

            ready = False
            for _ in range(40):
                if proc.poll() is not None:
                    break
                if self._local_port_open(port):
                    ready = True
                    break
                time.sleep(0.25)

            if not ready:
                exit_code = proc.poll()
                self.set_status("SSH tunnel failed — see log")
                self.ui(self._set_tunnel_state, "Connection failed", "error")
                self.logline(f"[SSH] Forward did not become ready (exit={exit_code}).")
                if proc.poll() is None:
                    proc.terminate()
                if self.ssh_proc is proc:
                    self.ssh_proc = None
                self.ui(self.tunnel_btn.configure, state="normal")
                return

            self.ui(self._set_tunnel_state, f"Active on {port}", "online")
            self.set_status(f"Tunnel active; connecting ADB to {target} …")
            self._connect_adb_target(target)
            self.ui(self.tunnel_btn.configure, state="normal")

        self.worker(job)

    def _watch_ssh_output(self, proc):
        try:
            if proc.stdout:
                for line in iter(proc.stdout.readline, ""):
                    if line:
                        self.logline("[SSH] " + line.rstrip())
        except Exception:
            pass
        finally:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if self.ssh_proc is proc and proc.poll() is not None:
                self.ssh_proc = None
                self.set_status(f"SSH tunnel stopped (exit {proc.returncode})")
                self.ui(self._set_tunnel_state, "Disconnected", "idle")

    def _connect_adb_target(self, target):
        result = adb_command("connect", target, timeout=40)
        self.logline(f"[ADB Connect] {result[1] or 'No response'} ({result[2]:.2f}s)")
        output_lower = result[1].lower()
        connected = result[0] == 0 and ("connected" in output_lower or "already" in output_lower)
        self.set_status(f"ADB connected to {target}" if connected else "ADB connection failed — see log")
        self._refresh_devices_job(preferred=target)

    def disconnect_tunnel(self):
        port = self.ssh_port or validated_port(self.port_var.get())
        target = f"{DEFAULT_IP}:{port}" if port else None
        proc = self.ssh_proc
        self.ssh_proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

        def job():
            if target:
                result = adb_command("disconnect", target, timeout=20)
                self.logline(f"[ADB Disconnect] {result[1] or 'done'}")
            self.set_status("SSH tunnel and local ADB connection stopped")
            self.ui(self._set_tunnel_state, "Disconnected", "idle")
        self.worker(job)

    # ---------- connection / mirror ----------
    def refresh_devices(self):
        self.status_var.set("Refreshing devices …")
        self.worker(self._refresh_devices_job)

    def _refresh_devices_job(self, preferred=None):
        rc, output, _elapsed = adb_command("devices", timeout=20)
        serials = []
        if rc == 0:
            for line in output.splitlines()[1:]:
                fields = line.strip().split()
                if len(fields) >= 2 and fields[1] == "device":
                    serials.append(fields[0])

        def update():
            old = self.device_var.get()
            self.devices = serials
            self.device_box["values"] = serials
            if preferred in serials:
                self.device_var.set(preferred)
            elif old in serials:
                self.device_var.set(old)
            elif serials:
                self.device_var.set(serials[0])
            else:
                self.device_var.set("")
            if serials:
                self.status_var.set(f"{len(serials)} device(s) connected")
            else:
                self.status_var.set("No authorized devices found")
                if output and rc != 0:
                    self.logline("[Devices] " + output)
        self.ui(update)

    def start_scrcpy(self):
        serial = self.require_serial()
        if not serial:
            return
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            self.status_var.set("Mirror is already running")
            return
        max_size, bitrate = STREAM_PROFILES.get(
            self.profile_var.get(), STREAM_PROFILES[DEFAULT_PROFILE])
        title = f"{APP_SHORT_NAME} Mirror {int(time.time())}"
        # Spawn far off-screen so the window is never seen loose before it is
        # re-parented into the mirror frame.
        command = [
            SCRCPY, "-s", serial,
            "--max-size", max_size,
            "--video-bit-rate", bitrate,
            "--window-title", title,
            "--window-borderless",
            "--window-x", "4000",
            "--window-y", "4000",
        ]
        try:
            self.scrcpy_proc = subprocess.Popen(command, creationflags=_NO_WINDOW)
            self.status_var.set(f"Mirror started: {self.profile_var.get()}")
            self.logline("[Mirror] " + command_text(command))
            proc = self.scrcpy_proc
            self.worker(lambda: self._watch_scrcpy_window(proc, title))
            self.after(1000, self._poll_scrcpy)
        except FileNotFoundError:
            messagebox.showerror(APP_NAME, "scrcpy was not found. Place scrcpy.exe beside this app or on PATH.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not start the mirror:\n{exc}")

    @staticmethod
    def _find_scrcpy_hwnd(proc, title):
        """Locate the scrcpy window by exact title, else by owning process id."""
        hwnd = _USER32.FindWindowW(None, title)
        if hwnd:
            return hwnd
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def enum_callback(handle, _lparam):
            pid = ctypes.c_ulong()
            _USER32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if pid.value == proc.pid and _USER32.IsWindowVisible(handle):
                found.append(handle)
                return 0
            return 1

        _USER32.EnumWindows(enum_callback, 0)
        return found[0] if found else 0

    def _watch_scrcpy_window(self, proc, title):
        """Find the scrcpy window and re-parent it into the mirror frame."""
        if _USER32 is None:
            return
        serial = self.active_serial()
        if serial:
            rc, output, _elapsed = adb_command("shell", "wm", "size",
                                               serial=serial, timeout=20)
            match = re.search(r"(\d+)x(\d+)", output) if rc == 0 else None
            if match:
                width, height = int(match.group(1)), int(match.group(2))
                if width and height:
                    self.mirror_aspect = width / height
        hwnd = 0
        for _ in range(600):  # up to ~60 s; remote tunnels are slow to first frame
            if proc.poll() is not None:
                return
            hwnd = self._find_scrcpy_hwnd(proc, title)
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            # Never leave an invisible window stranded off-screen.
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            self.set_status("Mirror window could not be embedded — press Start mirror again")
            self.logline("[Mirror] scrcpy window was not found; the stray process was closed.")
            return
        self.ui(self._adopt_scrcpy_window, hwnd)

    def _adopt_scrcpy_window(self, hwnd):
        if not (self.scrcpy_proc and self.scrcpy_proc.poll() is None):
            return
        try:
            self.mirror_frame.update_idletasks()
            _USER32.SetWindowLongW(hwnd, _GWL_STYLE, _WS_CHILD | _WS_VISIBLE)
            _USER32.SetParent(hwnd, self.mirror_frame.winfo_id())
            self.scrcpy_hwnd = hwnd
            self.mirror_hint.place_forget()
            self._resize_embedded_mirror()
            self.logline("[Mirror] scrcpy embedded into the CDC window.")
        except Exception as exc:
            self.scrcpy_hwnd = None
            self.logline(f"[Mirror] Could not embed scrcpy ({exc}); it stays a separate window.")

    def _resize_embedded_mirror(self):
        if not (self.scrcpy_hwnd and _USER32):
            return
        frame_w = max(self.mirror_frame.winfo_width(), 1)
        frame_h = max(self.mirror_frame.winfo_height(), 1)
        x = y = 0
        width, height = frame_w, frame_h
        if self.mirror_aspect:
            # Fit the device's aspect ratio inside the frame, centered.
            width = min(frame_w, int(frame_h * self.mirror_aspect))
            height = int(width / self.mirror_aspect)
            if height > frame_h:
                height = frame_h
                width = int(frame_h * self.mirror_aspect)
            x = (frame_w - width) // 2
            y = (frame_h - height) // 2
        try:
            _USER32.MoveWindow(self.scrcpy_hwnd, x, y, max(width, 1), max(height, 1), True)
        except Exception:
            pass

    def _clear_mirror_embed(self):
        self.scrcpy_hwnd = None
        self.mirror_aspect = None
        try:
            self.mirror_hint.place(relx=0.5, rely=0.5, anchor="center")
        except tk.TclError:
            pass

    def _poll_scrcpy(self):
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            self.after(1000, self._poll_scrcpy)
        elif self.scrcpy_proc:
            code = self.scrcpy_proc.returncode
            self.scrcpy_proc = None
            self._clear_mirror_embed()
            self.status_var.set(f"Mirror stopped (exit {code})")

    def stop_scrcpy(self):
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            self.scrcpy_proc.terminate()
            self.scrcpy_proc = None
            self._clear_mirror_embed()
            self.status_var.set("Mirror stopped")
        else:
            self.status_var.set("Mirror is not running")

    # ---------- install / store ----------
    def install_apk(self):
        serial = self.require_serial()
        if not serial:
            return
        paths = filedialog.askopenfilenames(
            title="Choose APK file(s)", filetypes=[("Android APK", "*.apk")])
        if not paths:
            return
        args = (["install", "-r", "-g", paths[0]] if len(paths) == 1 else
                ["install-multiple", "-r", "-g", *paths])
        self.status_var.set(f"Installing {len(paths)} APK file(s) …")

        def job():
            result = adb_command(*args, serial=serial, timeout=600)
            self.log_adb_result("Install APK", serial, args, result)
            rc, output, _elapsed = result
            success = rc == 0 and "Failure" not in output and "Error" not in output
            self.set_status("APK installed successfully" if success else "APK installation failed — see log")
            if success:
                self.ui(messagebox.showinfo, APP_NAME, "APK installed successfully on the selected device.")
            else:
                self.ui(messagebox.showerror, APP_NAME,
                        "APK installation failed. The detailed ADB result is in the app log.")
        self.worker(job)

    def open_convrse_store(self):
        serial = self.require_serial()
        if not serial:
            return
        args = ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", CONVRSE_STORE_URL]
        self.status_var.set("Opening Convrse Store on the device …")

        def job():
            result = adb_command(*args, serial=serial, timeout=30)
            self.log_adb_result("Convrse Store", serial, args, result)
            self.set_status("Convrse Store opened" if result[0] == 0 else "Could not open Convrse Store")
        self.worker(job)

    def open_cleanup_app(self):
        serial = self.require_serial()
        if not serial:
            return
        self.status_var.set("Opening CleanUp …")

        def job():
            installed = adb_command("shell", "pm", "path", CLEANUP_PACKAGE,
                                    serial=serial, timeout=20)
            if installed[0] != 0 or not installed[1].startswith("package:"):
                self.set_status("CleanUp is not installed")
                self.ui(messagebox.showwarning, APP_NAME,
                        f"CleanUp is not installed on the selected device.\n\n{CLEANUP_PACKAGE}")
                return
            args = ["shell", "monkey", "-p", CLEANUP_PACKAGE, "-c",
                    "android.intent.category.LAUNCHER", "1"]
            result = adb_command(*args, serial=serial, timeout=30)
            self.log_adb_result("Open CleanUp", serial, args, result)
            launched = result[0] == 0 and "No activities found" not in result[1]
            self.set_status("CleanUp opened" if launched else "Could not open CleanUp")
        self.worker(job)

    # ---------- remote ----------
    def send_keyevent(self, code):
        serial = self.require_serial()
        if not serial:
            return
        self.input_action_count += 1
        args = ["shell", "input", "keyevent", str(code)]

        def job():
            result = adb_command(*args, serial=serial, timeout=20)
            if result[0] != 0:
                self.log_adb_result(f"Keyevent {code}", serial, args, result)
                self.set_status(f"Keyevent {code} failed")
        self.worker(job)

    def send_custom_keyevent(self):
        code = self.key_var.get().strip()
        if not code.isdigit():
            messagebox.showwarning(APP_NAME, "Enter a numeric Android keyevent code, for example 66.")
            return
        self.send_keyevent(code)

    def screenshot(self):
        serial = self.require_serial()
        if not serial:
            return
        default = f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
        path = filedialog.asksaveasfilename(
            title="Save screenshot", initialfile=default, defaultextension=".png",
            filetypes=[("PNG image", "*.png")])
        if not path:
            return
        self.status_var.set("Capturing screenshot …")

        def job():
            command = [ADB, "-s", serial, "exec-out", "screencap", "-p"]
            started = time.monotonic()
            try:
                proc = subprocess.run(command, capture_output=True, timeout=40,
                                      creationflags=_NO_WINDOW)
                if proc.returncode == 0 and proc.stdout.startswith(b"\x89PNG"):
                    with open(path, "wb") as handle:
                        handle.write(proc.stdout)
                    self.set_status("Screenshot saved: " + os.path.basename(path))
                    self.logline(f"[Screenshot] {path} ({time.monotonic() - started:.2f}s)")
                else:
                    error = (proc.stderr or b"").decode(errors="replace")
                    self.set_status("Screenshot failed")
                    self.logline("[Screenshot] " + (error or "Invalid PNG response"))
            except Exception as exc:
                self.set_status("Screenshot failed")
                self.logline("[Screenshot] " + str(exc))
        self.worker(job)

    def open_device_settings(self):
        serial = self.require_serial()
        if not serial:
            return
        self.status_var.set("Opening Settings on the device …")

        def job():
            # Standard intent first; some TV boxes (like the H96) only expose
            # the Settings component directly.
            result = adb_command("shell", "am", "start", "-a",
                                 "android.settings.SETTINGS", serial=serial, timeout=20)
            if not (result[0] == 0 and "Error" not in result[1]):
                result = adb_command("shell", "am", "start", "-n",
                                     "com.android.tv.settings/.MainSettings",
                                     serial=serial, timeout=20)
            if result[0] == 0 and "Error" not in result[1]:
                self.set_status("Settings opened on the device")
            else:
                self.set_status("Could not open Settings — see log")
                self.logline("[Open Settings] " + (result[1] or "no output"))
        self.worker(job)

    # ---------- foreground package / recovery ----------
    def detect_foreground_package(self, serial):
        commands = [
            ("shell", "dumpsys", "window", "windows"),
            ("shell", "dumpsys", "activity", "activities"),
            ("shell", "dumpsys", "activity", "top"),
        ]
        combined = []
        for args in commands:
            rc, output, _elapsed = adb_command(*args, serial=serial, timeout=30)
            if output:
                combined.append(output)
            if rc == 0:
                package = parse_component_package(output)
                if package:
                    return package, "\n".join(combined)
        return None, "\n".join(combined)

    def _home_package(self, serial):
        rc, output, _elapsed = adb_command(
            "shell", "cmd", "package", "resolve-activity", "--brief",
            "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME",
            serial=serial, timeout=20)
        if rc == 0:
            match = re.search(r"([A-Za-z0-9_.$]+)/", output)
            if match:
                return match.group(1)
        return None

    def _package_is_protected(self, package, serial):
        return package in PROTECTED_PACKAGES or package == self._home_package(serial)

    def detect_current_async(self, serial, callback):
        self.status_var.set("Detecting the foreground application …")

        def job():
            package, diagnostics = self.detect_foreground_package(serial)
            if package:
                self.ui(self.current_app_var.set, f"Foreground package: {package}")
                self.set_status(f"Foreground app: {package}")
                self.ui(callback, package)
            else:
                self.logline("[Foreground detection failed]\n" + (diagnostics[-4000:] or "No dumpsys output"))
                self.set_status("Could not detect the foreground application")
                self.ui(messagebox.showerror, APP_NAME,
                        "The foreground app package could not be detected. No action was performed.")
        self.worker(job)

    def force_stop_current(self):
        serial = self.require_serial()
        if not serial:
            return

        def detected(package):
            if self._package_is_protected(package, serial):
                messagebox.showwarning(APP_NAME, f"Protected system/launcher package blocked:\n{package}")
                return
            self._force_stop(serial, package)
        self.detect_current_async(serial, detected)

    def _force_stop(self, serial, package):
        args = ["shell", "am", "force-stop", package]
        self.status_var.set(f"Force stopping {package} …")

        def job():
            result = adb_command(*args, serial=serial, timeout=30)
            self.log_adb_result("Force Stop", serial, args, result)
            self.set_status(f"Force stopped {package}" if result[0] == 0 else "Force stop failed")
        self.worker(job)

    def restart_current(self):
        serial = self.require_serial()
        if not serial:
            return

        def detected(package):
            if self._package_is_protected(package, serial):
                messagebox.showwarning(APP_NAME, f"Protected system/launcher package blocked:\n{package}")
                return
            self._restart_package(serial, package)
        self.detect_current_async(serial, detected)

    def _restart_package(self, serial, package):
        self.status_var.set(f"Restarting {package} …")

        def job():
            stop_args = ["shell", "am", "force-stop", package]
            stop_result = adb_command(*stop_args, serial=serial, timeout=30)
            self.log_adb_result("Restart: force-stop", serial, stop_args, stop_result)
            if stop_result[0] != 0:
                self.set_status("Restart failed during force-stop")
                return
            launch_args = ["shell", "monkey", "-p", package, "-c",
                           "android.intent.category.LAUNCHER", "1"]
            launch_result = adb_command(*launch_args, serial=serial, timeout=45)
            self.log_adb_result("Restart: launch", serial, launch_args, launch_result)
            launched = launch_result[0] == 0 and "No activities found" not in launch_result[1]
            if launched:
                time.sleep(0.8)
                _rc, pid_text, _elapsed = adb_command("shell", "pidof", package,
                                                      serial=serial, timeout=10)
                self._remember_capture_pids(pid_text)
            self.set_status(f"Restarted {package}" if launched else "App has no launchable activity or launch failed")
        self.worker(job)

    def clear_current_data(self):
        serial = self.require_serial()
        if not serial:
            return

        def detected(package):
            if self._package_is_protected(package, serial):
                messagebox.showwarning(APP_NAME, f"Protected system/launcher package blocked:\n{package}")
                return
            confirmed = messagebox.askyesno(
                APP_NAME,
                f"Clear ALL data for the running app?\n\nPackage: {package}\n\n"
                "This removes login, preferences, databases, files and cache. This cannot be undone.",
                icon="warning",
            )
            if confirmed:
                self._pm_clear(serial, package)
        self.detect_current_async(serial, detected)

    def _pm_clear(self, serial, package):
        args = ["shell", "pm", "clear", package]
        self.status_var.set(f"Clearing all data for {package} …")

        def job():
            result = adb_command(*args, serial=serial, timeout=60)
            self.log_adb_result("Clear Data (pm clear)", serial, args, result)
            success = result[0] == 0 and "Success" in result[1]
            self.set_status(f"Cleared all data for {package}" if success else "Clear data failed")
            if success:
                self.ui(messagebox.showinfo, APP_NAME, f"All app data was cleared for:\n{package}")
            else:
                self.ui(messagebox.showerror, APP_NAME, "Android did not clear the app data. See the command log.")
        self.worker(job)

    def clear_current_cache(self):
        serial = self.require_serial()
        if not serial:
            return

        def detected(package):
            if self._package_is_protected(package, serial):
                messagebox.showwarning(APP_NAME, f"Protected system/launcher package blocked:\n{package}")
                return
            self._clear_cache(serial, package)
        self.detect_current_async(serial, detected)

    def _clear_cache(self, serial, package):
        self.status_var.set(f"Clearing cache for {package} …")

        def job():
            # Debug builds allow a direct cache wipe via run-as.
            run_as_args = ["shell", "run-as", package, "sh", "-c",
                           "rm -rf cache/* code_cache/*"]
            result = adb_command(*run_as_args, serial=serial, timeout=45)
            self.log_adb_result("Clear Cache: run-as", serial, run_as_args, result)
            if result[0] == 0 and "not debuggable" not in result[1].lower():
                self.set_status(f"Cache cleared for {package}")
                return
            # Release builds: pm clear --cache-only, guarded by a device-side
            # timeout because this firmware can hang on it forever.
            pm_args = ["shell", f"timeout 10 pm clear --cache-only {package}"]
            result = adb_command(*pm_args, serial=serial, timeout=40)
            self.log_adb_result("Clear Cache: pm cache-only", serial, pm_args, result)
            if "Success" in result[1]:
                self.set_status(f"Cache cleared for {package}")
                return
            # Last resort: ask Android to reclaim cache space device-wide.
            trim_args = ["shell", "pm", "trim-caches", "512G"]
            trim = adb_command(*trim_args, serial=serial, timeout=180)
            self.log_adb_result("Clear Cache: trim fallback", serial, trim_args, trim)
            if trim[0] == 0 and "Error" not in trim[1]:
                self.set_status("Per-app cache clear unsupported here — trimmed ALL app caches instead")
            else:
                self.set_status("Cache clear failed — see log")
        self.worker(job)

    def close_background_apps(self):
        serial = self.require_serial()
        if not serial:
            return
        if not messagebox.askyesno(
                APP_NAME,
                "Close background applications and their leftover tasks?\n\n"
                "The foreground app and launcher will remain protected."):
            return
        self.status_var.set("Closing background apps and tasks …")

        def job():
            # This media player has no recents UI, so stale tasks pile up
            # invisibly — remove them, then kill cached background processes.
            rc, output, _elapsed = adb_command("shell", "am", "stack", "list",
                                               serial=serial, timeout=45)
            closed = []
            if rc == 0:
                entries = re.findall(
                    r"taskId=(\d+): ([A-Za-z0-9_.$]+)/\S+.*?visible=(true|false)", output)
                home = self._home_package(serial)
                for task_id, package, visible in entries:
                    if visible == "true" or package == home or package in PROTECTED_PACKAGES:
                        continue
                    adb_command("shell", "am", "stack", "remove", task_id,
                                serial=serial, timeout=20)
                    adb_command("shell", "am", "force-stop", package,
                                serial=serial, timeout=20)
                    closed.append(f"{package} (task {task_id})")
            else:
                self.logline("[Close Background] am stack list failed:\n" + output)
            kill = adb_command("shell", "am", "kill-all", serial=serial, timeout=30)
            if closed:
                self.logline("[Close Background] Removed:\n" + "\n".join(closed))
            if kill[0] == 0 or closed:
                self.set_status(f"Closed {len(closed)} background task(s); cached processes killed")
            else:
                self.set_status("Could not close background apps — see log")
        self.worker(job)

    # ---------- logs ----------
    def _package_metadata(self, serial, package):
        sections = []
        commands = [
            ("Package", ("shell", "dumpsys", "package", package)),
            ("Activity", ("shell", "dumpsys", "activity", "top")),
            ("Device", ("shell", "getprop")),
        ]
        for label, args in commands:
            rc, output, elapsed = adb_command(*args, serial=serial, timeout=60)
            sections.append(f"===== {label} (rc={rc}, {elapsed:.2f}s) =====\n{output}\n")
        return "\n".join(sections)

    def _current_pids(self, serial, package):
        _rc, output, _elapsed = adb_command("shell", "pidof", package,
                                            serial=serial, timeout=15)
        return {item for item in output.split() if item.isdigit()}

    def _remember_capture_pids(self, pid_text):
        for item in (pid_text or "").split():
            if item.isdigit():
                self.capture_pids.add(item)

    @staticmethod
    def _filter_app_logs(raw, package, pids):
        kept = []
        pid_patterns = [re.compile(rf"\s{re.escape(pid)}\s+") for pid in pids]
        important = ("AndroidRuntime", "FATAL EXCEPTION", "ActivityManager",
                     "ActivityTaskManager", "am_crash", "ANR in")
        for line in raw.splitlines():
            package_related = package.lower() in line.lower()
            pid_related = any(pattern.search(line) for pattern in pid_patterns)
            system_related = any(token in line for token in important) and package_related
            if package_related or pid_related or system_related:
                kept.append(line)
        if not kept:
            kept.append("No package-specific log lines were found in the captured buffers.")
        return "\n".join(kept) + "\n"

    def save_recent_logs(self):
        serial = self.require_serial()
        if not serial:
            return

        def detected(package):
            default = f"{package}-logs-{datetime.now():%Y%m%d-%H%M%S}.txt"
            path = filedialog.asksaveasfilename(
                title="Save recent app logs", initialfile=default,
                defaultextension=".txt", filetypes=[("Text log", "*.txt")])
            if path:
                self._save_recent_logs_job(serial, package, path)
        self.detect_current_async(serial, detected)

    def _save_recent_logs_job(self, serial, package, path):
        self.status_var.set(f"Collecting recent logs for {package} …")

        def job():
            pids = self._current_pids(serial, package)
            rc, raw, elapsed = adb_command(
                "logcat", "-d", "-v", "threadtime", "-b", "main", "-b", "system",
                "-b", "crash", serial=serial, timeout=120)
            focused = self._filter_app_logs(raw, package, pids)
            metadata = self._package_metadata(serial, package)
            header = (
                f"Package: {package}\nDevice: {serial}\nCaptured: {datetime.now().isoformat()}\n"
                f"Logcat: rc={rc}, {elapsed:.2f}s\nPIDs: {', '.join(sorted(pids)) or 'not running'}\n\n"
            )
            try:
                with open(path, "w", encoding="utf-8", errors="replace") as handle:
                    handle.write(header)
                    handle.write("===== FOCUSED LOGCAT =====\n")
                    handle.write(focused)
                    handle.write("\n===== DIAGNOSTICS =====\n")
                    handle.write(metadata)
                self.set_status("Recent app logs saved")
                self.logline(f"[Save Recent Logs] {path}")
            except Exception as exc:
                self.set_status("Could not save recent logs")
                self.logline("[Save Recent Logs] " + str(exc))
        self.worker(job)

    def toggle_debug_capture(self):
        if self.capture_proc:
            self.stop_debug_capture()
            return
        self.start_auto_session()

    def start_auto_session(self):
        serial = self.require_serial()
        if not serial:
            return
        self.detect_current_async(serial, lambda package: self.start_debug_capture(serial, package))

    def start_debug_capture(self, serial, package):
        if self.capture_proc:
            return
        self.capture_dir = tempfile.mkdtemp(prefix="cdc-session-")
        raw_path = os.path.join(self.capture_dir, "session-full.log")
        try:
            self.capture_handle = open(raw_path, "wb")
            command = [ADB, "-s", serial, "logcat", "-T", "1", "-v", "threadtime",
                       "-b", "main", "-b", "system", "-b", "crash", "-b", "events"]
            self.capture_proc = subprocess.Popen(
                command, stdout=self.capture_handle, stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW)
            self.capture_package = package
            self.capture_serial = serial
            self.capture_packages = {package}
            self.capture_pids = self._current_pids(serial, package)
            self.capture_started = datetime.now()
            self.capture_deadline = time.monotonic() + SESSION_SECONDS
            self.capture_stop_event.clear()
            self.last_network_totals = {}

            metrics_path = os.path.join(self.capture_dir, "metrics.csv")
            self.capture_metrics_handle = open(metrics_path, "w", newline="", encoding="utf-8")
            writer = csv.writer(self.capture_metrics_handle)
            writer.writerow([
                "timestamp", "foreground_package", "pids", "pss_kb", "rss_kb", "threads",
                "eth0_rx_bytes", "eth0_tx_bytes", "eth0_rx_drop", "eth0_tx_drop",
                "wlan0_rx_bytes", "wlan0_tx_bytes", "wlan0_rx_drop", "wlan0_tx_drop",
                "cdc_remote_actions",
            ])
            self.capture_metrics_handle.flush()

            input_path = os.path.join(self.capture_dir, "input-events.log")
            self.session_touch_downs = 0
            self.session_tracking_downs = 0
            self.session_clicks = 0
            self.session_keys = 0
            input_filter = "getevent -lt | grep -E 'EV_KEY|BTN_|ABS_MT_TRACKING_ID'"
            self.capture_input_proc = subprocess.Popen(
                [ADB, "-s", serial, "shell", "sh", "-c", input_filter],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, errors="replace", bufsize=1, creationflags=_NO_WINDOW)
            input_proc = self.capture_input_proc
            self.worker(lambda: self._session_input_reader(input_proc, input_path))

            self.capture_metrics_thread = threading.Thread(
                target=self._metrics_loop, args=(serial,), daemon=True)
            self.capture_metrics_thread.start()
            self.capture_btn.configure(text="Stop session · 30:00")
            self.session_status_var.set("Diagnostic session · 30:00")
            self.status_var.set(f"30-minute diagnostic session started for {package}")
            self.logline(f"[CDC Session] Started for {package} on {serial}")
            self._update_session_countdown()
        except Exception as exc:
            if self.capture_handle:
                self.capture_handle.close()
            self.capture_handle = None
            self.capture_proc = None
            self.status_var.set("Could not start diagnostic session")
            messagebox.showerror(APP_NAME, f"Could not start diagnostic session:\n{exc}")

    def _session_input_reader(self, proc, path):
        """Stream device input events to disk and count user interactions live.

        Counts physical input only (touch panel, RC remote); scrcpy-injected
        events never reach /dev/input, so visitor counts stay clean.
        """
        try:
            with open(path, "w", encoding="utf-8", errors="replace") as handle:
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    handle.write(line)
                    if " DOWN" in line:
                        if "BTN_TOUCH" in line:
                            self.session_touch_downs += 1
                        elif "BTN_LEFT" in line or "BTN_MOUSE" in line:
                            self.session_clicks += 1
                        elif " KEY_" in line:
                            self.session_keys += 1
                    elif "ABS_MT_TRACKING_ID" in line and not line.rstrip().endswith("ffffffff"):
                        self.session_tracking_downs += 1
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def session_touch_total(self):
        """Touch panels report BTN_TOUCH or type-B tracking IDs, not both."""
        return self.session_touch_downs or self.session_tracking_downs

    @staticmethod
    def _parse_network_totals(output):
        totals = {}
        for line in (output or "").splitlines():
            if ":" not in line:
                continue
            interface, data = line.split(":", 1)
            fields = data.split()
            if len(fields) >= 12:
                totals[interface.strip()] = {
                    "rx": int(fields[0]), "rx_drop": int(fields[3]),
                    "tx": int(fields[8]), "tx_drop": int(fields[11]),
                }
        return totals

    def _metrics_loop(self, serial):
        while not self.capture_stop_event.is_set() and self.capture_proc:
            timestamp = datetime.now().isoformat(timespec="seconds")
            package, _diagnostics = self.detect_foreground_package(serial)
            if package:
                self.capture_package = package
                self.capture_packages.add(package)
                self.ui(self.current_app_var.set, f"Foreground package: {package}")
            else:
                package = self.capture_package or "unknown"

            pids = self._current_pids(serial, package) if package != "unknown" else set()
            self.capture_pids.update(pids)
            pid = sorted(pids)[0] if pids else None
            pss_kb = rss_kb = threads = ""
            if pid:
                _rc, proc_status, _elapsed = adb_command(
                    "shell", "cat", f"/proc/{pid}/status", serial=serial, timeout=15)
                rss_match = re.search(r"^VmRSS:\s+(\d+)", proc_status, re.MULTILINE)
                thread_match = re.search(r"^Threads:\s+(\d+)", proc_status, re.MULTILINE)
                rss_kb = rss_match.group(1) if rss_match else ""
                threads = thread_match.group(1) if thread_match else ""
                _rc, meminfo, _elapsed = adb_command(
                    "shell", "dumpsys", "meminfo", package, serial=serial, timeout=25)
                pss_match = re.search(r"TOTAL PSS:\s+(\d+)", meminfo)
                if not pss_match:
                    pss_match = re.search(r"^\s*TOTAL\s+(\d+)", meminfo, re.MULTILINE)
                pss_kb = pss_match.group(1) if pss_match else ""

            _rc, net_output, _elapsed = adb_command(
                "shell", "cat", "/proc/net/dev", serial=serial, timeout=15)
            network = self._parse_network_totals(net_output)
            eth = network.get("eth0", {})
            wifi = network.get("wlan0", {})
            values = [
                timestamp, package, " ".join(sorted(pids)), pss_kb, rss_kb, threads,
                eth.get("rx", ""), eth.get("tx", ""),
                eth.get("rx_drop", ""), eth.get("tx_drop", ""),
                wifi.get("rx", ""), wifi.get("tx", ""),
                wifi.get("rx_drop", ""), wifi.get("tx_drop", ""),
                self.input_action_count,
            ]
            try:
                csv.writer(self.capture_metrics_handle).writerow(values)
                self.capture_metrics_handle.flush()
            except Exception:
                break

            active = "eth0" if eth.get("rx", 0) or eth.get("tx", 0) else "wlan0"
            active_values = network.get(active, {})
            total = active_values.get("rx", 0) + active_values.get("tx", 0)
            previous = self.last_network_totals.get(active)
            kbps = ((total - previous) * 8 / SESSION_SAMPLE_SECONDS / 1000) \
                if previous is not None else 0
            self.last_network_totals[active] = total
            self.ui(self.session_detail_var.set,
                    f"Target: {package} | {active} {kbps:.1f} Kbps | "
                    f"Touches {self.session_touch_total()} | Clicks {self.session_clicks} | "
                    f"Keys {self.session_keys} | CDC inputs {self.input_action_count}")
            self.capture_stop_event.wait(SESSION_SAMPLE_SECONDS)

    def _update_session_countdown(self):
        if not self.capture_proc or self.capture_deadline is None:
            return
        remaining = max(0, int(self.capture_deadline - time.monotonic()))
        minutes, seconds = divmod(remaining, 60)
        self.session_status_var.set(f"Diagnostic session · {minutes:02d}:{seconds:02d}")
        self.capture_btn.configure(text=f"Stop session · {minutes:02d}:{seconds:02d}")
        if remaining <= 0:
            self.stop_debug_capture()
            return
        self.capture_timer_id = self.after(1000, self._update_session_countdown)

    def stop_debug_capture(self):
        proc = self.capture_proc
        if not proc:
            return
        package = self.capture_package or "system-session"
        serial = self.capture_serial
        capture_dir = self.capture_dir
        started = self.capture_started
        pids = set(self.capture_pids)
        packages = set(self.capture_packages)
        self.capture_stop_event.set()
        self.capture_proc = None
        if self.capture_timer_id:
            try:
                self.after_cancel(self.capture_timer_id)
            except Exception:
                pass
            self.capture_timer_id = None
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if self.capture_handle:
            self.capture_handle.close()
        self.capture_handle = None
        if self.capture_input_proc and self.capture_input_proc.poll() is None:
            try:
                self.capture_input_proc.terminate()
                self.capture_input_proc.wait(timeout=3)
            except Exception:
                try:
                    self.capture_input_proc.kill()
                except Exception:
                    pass
        self.capture_input_proc = None
        if self.capture_input_handle:
            self.capture_input_handle.close()
        self.capture_input_handle = None
        if self.capture_metrics_thread and self.capture_metrics_thread.is_alive():
            self.capture_metrics_thread.join(timeout=3)
        if self.capture_metrics_handle:
            self.capture_metrics_handle.close()
        self.capture_metrics_handle = None
        pids.update(self._current_pids(serial, package))
        self.capture_btn.configure(text="Start diagnostic session")

        self.session_status_var.set("Saving diagnostic session…")
        output_dir = os.path.join(os.path.expanduser("~"), "Documents", "CDC Sessions")
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError:
            output_dir = app_dir()
        path = os.path.join(
            output_dir, f"CDC-{package}-{datetime.now():%Y%m%d-%H%M%S}.zip")
        self.status_var.set("Building CDC diagnostic ZIP …")

        def job():
            time.sleep(1.5)  # let the input reader thread flush and exit
            raw_path = os.path.join(capture_dir, "session-full.log")
            try:
                with open(raw_path, "r", encoding="utf-8", errors="replace") as handle:
                    raw = handle.read()
                focused_sections = []
                for observed_package in sorted(packages):
                    focused_sections.append(
                        f"===== {observed_package} =====\n" +
                        self._filter_app_logs(raw, observed_package, pids))
                focused = "\n".join(focused_sections)
                focused_path = os.path.join(capture_dir, "app-focused.log")
                with open(focused_path, "w", encoding="utf-8") as handle:
                    handle.write(focused)
                metadata = self._package_metadata(serial, package)
                metadata_path = os.path.join(capture_dir, "diagnostics.txt")
                with open(metadata_path, "w", encoding="utf-8", errors="replace") as handle:
                    handle.write(metadata)
                crash_lines = [line for line in raw.splitlines() if any(token in line for token in (
                    "FATAL EXCEPTION", "Fatal signal", "am_crash", "ANR in",
                    "ReactNativeJS", "Failed to compile"))]
                crash_count = sum(1 for line in crash_lines
                                  if "FATAL EXCEPTION" in line or "Fatal signal" in line)
                anr_count = sum(1 for line in crash_lines if "ANR in" in line)
                crash_path = os.path.join(capture_dir, "crash-summary.log")
                with open(crash_path, "w", encoding="utf-8", errors="replace") as handle:
                    handle.write("\n".join(crash_lines) or "No crash/ANR markers detected.")

                stopped = datetime.now()
                touches = self.session_touch_total()
                summary_path = os.path.join(capture_dir, "capture-summary.txt")
                with open(summary_path, "w", encoding="utf-8") as handle:
                    handle.write(
                        f"Product: {APP_NAME}\nPrimary package: {package}\nDevice: {serial}\n"
                        f"Observed packages: {', '.join(sorted(packages))}\n"
                        f"Started: {started.isoformat()}\n"
                        f"Stopped: {stopped.isoformat()}\n"
                        f"Observed PIDs: {', '.join(sorted(pids)) or 'none'}\n"
                        f"Device touches: {touches}\n"
                        f"Remote-mouse clicks: {self.session_clicks}\n"
                        f"Remote key presses: {self.session_keys}\n"
                        f"CDC remote actions: {self.input_action_count}\n"
                        f"Crashes: {crash_count}\nANRs: {anr_count}\n"
                        "session-full.log may include system and other-app messages emitted during this session.\n"
                    )
                master_path = os.path.join(output_dir, "sessions-master.csv")
                new_master = not os.path.isfile(master_path)
                with open(master_path, "a", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    if new_master:
                        writer.writerow(["date", "start", "end", "device", "package",
                                         "touches", "mouse_clicks", "key_presses",
                                         "crashes", "anrs", "zip"])
                    writer.writerow([f"{started:%Y-%m-%d}", f"{started:%H:%M:%S}",
                                     f"{stopped:%H:%M:%S}", serial, package,
                                     touches, self.session_clicks, self.session_keys,
                                     crash_count, anr_count, os.path.basename(path)])
                self._capture_session_screenshot(serial, capture_dir)
                with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name in ("capture-summary.txt", "app-focused.log", "session-full.log",
                                 "diagnostics.txt", "metrics.csv", "input-events.log",
                                 "crash-summary.log", "final-screen.png"):
                        source = os.path.join(capture_dir, name)
                        if os.path.isfile(source):
                            archive.write(source, arcname=name)
                verdict = ("no crashes" if crash_count == 0 and anr_count == 0
                           else f"{crash_count} crash(es), {anr_count} ANR(s)")
                self.set_status(f"CDC diagnostic session saved — {touches} touches, {verdict}")
                self.ui(self.session_status_var.set, f"Session saved · {verdict}")
                self.ui(self.session_detail_var.set, path)
                self.logline(f"[CDC Session] Saved {path} — touches {touches}, "
                             f"clicks {self.session_clicks}, keys {self.session_keys}, {verdict}")
                shutil.rmtree(capture_dir, ignore_errors=True)
            except Exception as exc:
                self.set_status("Could not build CDC session ZIP")
                self.ui(self.session_status_var.set, "Session save failed")
                self.logline(f"[CDC Session] {exc}; temporary files: {capture_dir}")
            finally:
                self._reset_capture_state()
        self.worker(job)

    def _capture_session_screenshot(self, serial, capture_dir):
        command = [ADB, "-s", serial, "exec-out", "screencap", "-p"]
        try:
            proc = subprocess.run(command, capture_output=True, timeout=30,
                                  creationflags=_NO_WINDOW)
            if proc.returncode == 0 and proc.stdout.startswith(b"\x89PNG"):
                with open(os.path.join(capture_dir, "final-screen.png"), "wb") as handle:
                    handle.write(proc.stdout)
        except Exception:
            pass

    def _reset_capture_state(self):
        self.capture_package = None
        self.capture_serial = None
        self.capture_dir = None
        self.capture_pids = set()
        self.capture_packages = set()
        self.capture_started = None
        self.capture_deadline = None
        self.capture_metrics_thread = None
        self.capture_stop_event.clear()

    # ---------- Claude ----------
    def _terminal_environment(self, serial=None):
        environment = os.environ.copy()
        environment["PATH"] = app_dir() + os.pathsep + environment.get("PATH", "")
        if serial:
            environment["ANDROID_SERIAL"] = serial
        return environment

    def run_claude(self):
        serial = self.active_serial()
        environment = self._terminal_environment(serial)
        working_directory = app_dir()
        if getattr(sys, "frozen", False) and os.path.basename(working_directory).lower() == "dist":
            working_directory = os.path.dirname(working_directory)
        try:
            if os.name == "nt":
                wt = shutil.which("wt.exe", path=environment.get("PATH"))
                if wt:
                    command = [wt, "-d", working_directory, "powershell.exe", "-NoExit",
                               "-Command", "claude"]
                else:
                    command = ["powershell.exe", "-NoExit", "-Command", "claude"]
                subprocess.Popen(command, cwd=working_directory, env=environment,
                                 creationflags=_NEW_CONSOLE)
            elif sys.platform == "darwin":
                script = f'tell application "Terminal" to do script "cd {working_directory} && claude"'
                subprocess.Popen(["osascript", "-e", script], env=environment)
            else:
                subprocess.Popen(["x-terminal-emulator", "-e", "claude"],
                                 cwd=working_directory, env=environment)
            self.status_var.set("Claude terminal opened" + (f" for {serial}" if serial else ""))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open Claude:\n{exc}")

    # ---------- close ----------
    def on_close(self):
        if self.capture_proc:
            if messagebox.askyesno(
                    APP_NAME,
                    "A CDC diagnostic session is recording.\n\nStop and save it now?"):
                self.stop_debug_capture()
                messagebox.showinfo(APP_NAME,
                                    "The diagnostic ZIP is being saved. Close CDC again when saving finishes.")
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
        self.destroy()


def main():
    ScrcpyRemote().mainloop()


if __name__ == "__main__":
    main()
