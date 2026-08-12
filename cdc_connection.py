#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Connection primitives for Convrse Device Control: key storage and ports.

Two field problems drove this module.

The SSH key had to be re-selected constantly, because the only record of it was
a settings string written when the window closed.  It now lives in a private
per-user directory with permissions OpenSSH will actually accept, imported once
from either a file or pasted text.

The ADB port was treated as if it identified a device.  It does not.  The CDM
website leases whichever port happens to be free when a tunnel is opened, so the
same physical device is 17000 one morning and 17003 the next, and two operators
who both assume 17000 end up driving the same screen.  The helpers here keep the
local socket and the leased remote port as separate values, and the caller is
expected to confirm the device identity over ADB before acting on anything.

Nothing in this module imports Qt, so it can be unit tested directly.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import subprocess
import sys


APP_VENDOR = "Convrse"
APP_FOLDER = "DeviceControl"
KEY_FILENAME = "cdm-key.pem"

# Local forwarding sockets are chosen from a high private range.  Staying away
# from the 17000 block the gateway leases keeps the two numbers visibly
# different in the UI and in the logs, which is the whole point.
LOCAL_PORT_RANGE = (49215, 49999)

_PRIVATE_KEY_MARKER = re.compile(
    r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(
    r"-----END (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")


class KeyImportError(ValueError):
    """The supplied text or file is not usable as an SSH private key."""


def app_data_dir() -> Path:
    """Per-user application directory, created on demand."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        root = Path(base) / APP_VENDOR / APP_FOLDER
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / f"{APP_VENDOR} {APP_FOLDER}"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        root = Path(base) / APP_VENDOR.lower() / APP_FOLDER.lower()
    root.mkdir(parents=True, exist_ok=True)
    return root


def key_dir() -> Path:
    path = app_data_dir() / "keys"
    path.mkdir(parents=True, exist_ok=True)
    return path


def stored_key_path() -> Path:
    return key_dir() / KEY_FILENAME


def has_stored_key() -> bool:
    path = stored_key_path()
    return path.is_file() and path.stat().st_size > 0


def normalize_key_text(text: str) -> str:
    """Validate pasted key material and return it with a trailing newline.

    OpenSSH rejects a key whose armour is damaged with an error that reads like
    a permissions problem, so the shape is checked here where a clear message
    can still be produced.
    """
    if not text or not text.strip():
        raise KeyImportError("No key text was supplied.")
    # Windows clipboards and chat apps routinely introduce CRLF or stray blanks.
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not _PRIVATE_KEY_MARKER.search(cleaned):
        raise KeyImportError(
            "That does not look like a private key. It must start with a line "
            "such as -----BEGIN RSA PRIVATE KEY-----.\n\n"
            "A key that starts with 'ssh-rsa' is the public half and will not work.")
    if not _PRIVATE_KEY_END.search(cleaned):
        raise KeyImportError(
            "The key is missing its closing -----END ... PRIVATE KEY----- line. "
            "Copy the whole file, including the first and last lines.")
    return cleaned + "\n"


def restrict_permissions(path: Path) -> None:
    """Lock the key to the current account.

    OpenSSH refuses to use a private key that other accounts can read and fails
    the connection rather than the key check, which is a confusing way to learn
    about a file permission.
    """
    if os.name == "nt":
        user = os.environ.get("USERNAME") or ""
        commands = [
            ["icacls", str(path), "/inheritance:r"],
            ["icacls", str(path), "/grant:r", f"{user}:(R,W)"],
        ]
        for command in commands:
            try:
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                # A key that works with loose permissions beats no key at all;
                # the caller surfaces any resulting SSH failure.
                pass
    else:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def import_key_text(text: str) -> Path:
    """Store pasted key material and return where it landed."""
    body = normalize_key_text(text)
    target = stored_key_path()
    target.write_text(body, encoding="ascii", newline="\n")
    restrict_permissions(target)
    return target


def import_key_file(source: str | os.PathLike) -> Path:
    """Copy a .pem the operator picked into private storage."""
    path = Path(os.path.expanduser(str(source).strip().strip('"')))
    if not path.is_file():
        raise KeyImportError(f"No such file:\n{path}")
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise KeyImportError(f"Could not read {path.name}: {exc}") from exc
    return import_key_text(text)


def forget_key() -> None:
    path = stored_key_path()
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def key_fingerprint_hint() -> str:
    """A short, non-secret label so the operator can tell two keys apart."""
    path = stored_key_path()
    if not path.is_file():
        return ""
    try:
        body = path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return ""
    payload = "".join(
        line.strip() for line in body.splitlines()
        if line.strip() and not line.startswith("-----"))
    if len(payload) < 16:
        return ""
    import hashlib
    digest = hashlib.sha256(payload.encode("ascii", "ignore")).hexdigest()
    return f"{digest[:4]}:{digest[4:8]}"


# ---------------------------------------------------------------- ports ----

def validated_port(value) -> int | None:
    text = str(value).strip()
    if not text.isdigit():
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


def parse_endpoint(value: str) -> int | None:
    """Accept what the CDM website shows, in whatever shape it was copied.

    Operators paste '17002', 'cdm.convrse.ai:17002', or a whole ssh command.
    All three mean the same leased remote port.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return validated_port(text)
    match = re.search(r":(\d{2,5})\b", text)
    if match:
        return validated_port(match.group(1))
    match = re.search(r"\b(\d{4,5})\b", text)
    if match:
        return validated_port(match.group(1))
    return None


def is_port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_free_local_port(
    preferred: int | None = None,
    host: str = "127.0.0.1",
    port_range: tuple[int, int] = LOCAL_PORT_RANGE,
) -> int:
    """Reserve a local socket for this operator's forward.

    Binding is the authoritative test.  Probing with a connect() only proves
    nobody is listening right now, which is exactly the race that let two
    operators share one tunnel.
    """
    if preferred and can_bind(preferred, host):
        return preferred
    low, high = port_range
    for port in range(low, high + 1):
        if can_bind(port, host):
            return port
    # Nothing free in the preferred window; let the OS pick anything.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def can_bind(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def build_ssh_command(
    ssh_executable: str,
    pem_path: str,
    local_port: int,
    remote_port: int,
    ssh_host: str,
) -> list[str]:
    """Forward a local socket to the port the gateway leased for this device.

    The V2.3 build forwarded ``{port}:localhost:{port}``, tying the local socket
    to the leased number.  Separating them is what lets two operators work at
    the same time without landing on one another's device.
    """
    # The forward target is 127.0.0.1 rather than "localhost".  On the gateway
    # localhost resolves to ::1, while the tunnel listeners bind 0.0.0.0, so
    # "localhost" makes ssh attempt IPv6 first and get refused before it falls
    # back to IPv4.  Naming the address removes the wasted attempt and the class
    # of failure that comes with it.
    return [
        ssh_executable,
        "-i", pem_path,
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-N",
        "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        ssh_host,
    ]


def describe_route(local_port: int | None, remote_port: int | None, host: str) -> str:
    """One-line rendering of the route, for the status strip and the log."""
    if not local_port or not remote_port:
        return "Not connected"
    gateway = host.split("@")[-1]
    return f"127.0.0.1:{local_port}  →  {gateway}:{remote_port}"


class DeviceIdentity:
    """What actually answered on the other end of the tunnel."""

    __slots__ = ("serial", "model", "name", "android", "project")

    def __init__(self, serial="", model="", name="", android="", project=""):
        self.serial = serial
        self.model = model
        self.name = name
        self.android = android
        self.project = project

    def __bool__(self):
        return bool(self.serial or self.model)

    def label(self) -> str:
        """Lead with the serial, because that is what the CDM website shows.

        The operator's check is "am I on the device I opened a tunnel for?", and
        the answer is on the portal as ``SN2026020201959 / neopolis``.  Leading
        with the hardware name instead put ``rk3576_box`` -- identical on every
        unit in the fleet -- where the distinguishing value should be.
        """
        if self.serial and self.project:
            return f"{self.serial} / {self.project}"
        if self.serial:
            return self.serial
        return self.model or "Unidentified device"

    def details(self) -> str:
        """Everything known, for a tooltip."""
        parts = []
        if self.serial:
            parts.append(f"Serial: {self.serial}")
        if self.project:
            parts.append(f"Project: {self.project}")
        if self.model:
            parts.append(f"Model: {self.model}")
        if self.name:
            parts.append(f"Hardware: {self.name}")
        if self.android:
            parts.append(f"Android: {self.android}")
        return "\n".join(parts)

    def __repr__(self):
        return f"DeviceIdentity(serial={self.serial!r}, model={self.model!r})"
