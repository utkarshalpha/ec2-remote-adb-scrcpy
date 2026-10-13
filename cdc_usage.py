#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live resource usage for one device, read over ADB.

The Usage panel answers the questions an operator otherwise has to guess at
from a stuttering mirror: is the box pegged, is it out of memory, is storage
full, is the network moving. Everything comes from one ``adb shell`` call per
tick, because every call crosses the SSH tunnel and a round trip per counter
would cost more than the counters are worth.

Rates are worked out from the change between two snapshots, and time is taken
from the device's own ``/proc/uptime`` rather than the host clock, so a slow
tunnel round trip does not show up as a dip or a spike.

Nothing here imports Qt, so it can be tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re


# One call, every section tagged. stderr is discarded on the device so a file
# the kernel does not expose cannot break the parse of the ones it does, and
# the big files are filtered there, because the reply crosses the tunnel.
USAGE_SCRIPT = (
    "exec 2>/dev/null;"
    "echo @@id;getprop ro.serialno;"
    "echo @@stat;grep '^cpu' /proc/stat;"
    "echo @@mem;grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached):' /proc/meminfo;"
    "echo @@net;cat /proc/net/dev;"
    "echo @@disk;grep -E ' (mmcblk[0-9]+|sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+) ' /proc/diskstats;"
    "echo @@df;df -k /data;"
    "echo @@up;cat /proc/uptime;"
    "echo @@freq;cat /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq;"
    "echo @@gpu;"
    "for f in /sys/class/devfreq/*gpu*/load /sys/class/devfreq/*gpu*/cur_freq"
    " /sys/class/kgsl/kgsl-3d0/gpu_busy_percentage"
    " /sys/class/kgsl/kgsl-3d0/devfreq/cur_freq"
    " /sys/class/misc/mali0/device/utilisation"
    " /sys/class/misc/mali0/device/utilization;"
    "do [ -r \"$f\" ]&&echo \"$f=$(cat \"$f\")\";done;"
    "echo @@temp;"
    "for z in /sys/class/thermal/thermal_zone*;"
    "do echo \"$(cat $z/type)=$(cat $z/temp)\";done;"
    "echo @@end"
)

# How many samples a graph keeps. At the 2s tick that is two minutes.
HISTORY_LENGTH = 60

SECTOR_BYTES = 512

# Interfaces that only carry traffic already counted on a real one, or none.
_VIRTUAL_INTERFACE_PREFIXES = (
    "lo", "dummy", "sit", "ip6tnl", "ip_vti", "ip6_vti", "ifb", "tunl",
    "gre", "erspan", "ip6gre", "bond", "teql",
)
# Whole disks only: partitions, boot areas, loop and device-mapper devices
# would count the same I/O several times over.
_WHOLE_DISK = re.compile(r"^(mmcblk\d+|sd[a-z]+|nvme\d+n\d+|vd[a-z]+)$")
_CPU_TEMP_ZONE = re.compile(r"soc|cpu|bigcore|littlecore|center|tsens", re.I)
_GPU_TEMP_ZONE = re.compile(r"gpu", re.I)
_LEADING_NUMBER = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


class UsageParseError(ValueError):
    """The device answered, but not with anything that looks like counters."""


@dataclass
class Snapshot:
    """Raw counters exactly as the device reported them at one instant."""

    serial: str = ""
    cpu: tuple[int, int] | None = None              # (total, idle) jiffies
    cores: list[tuple[int, int]] = field(default_factory=list)
    meminfo: dict[str, int] = field(default_factory=dict)   # kB
    net: dict[str, tuple[int, int]] = field(default_factory=dict)   # rx, tx bytes
    disks: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    storage: tuple[int, int, int, str] | None = None  # total, used, free kB, mount
    uptime: float | None = None
    freqs_khz: list[int] = field(default_factory=list)
    gpu: dict[str, str] = field(default_factory=dict)
    temps: dict[str, float] = field(default_factory=dict)   # zone type -> °C


@dataclass
class UsageSample:
    """What the panel shows. ``None`` means "not known", never zero."""

    serial: str = ""
    # True when this reply came from a different device than the last one on
    # the same address: the gateway re-leased the port. History must restart.
    device_changed: bool = False
    cpu_percent: float | None = None
    core_percents: list[float] = field(default_factory=list)
    cpu_cores: int = 0
    cpu_freq_mhz: float | None = None
    cpu_temp_c: float | None = None
    uptime_s: float | None = None

    gpu_percent: float | None = None
    gpu_freq_mhz: float | None = None
    gpu_temp_c: float | None = None

    mem_total: int | None = None          # bytes
    mem_used: int | None = None
    mem_available: int | None = None
    mem_cached: int | None = None

    storage_total: int | None = None      # bytes
    storage_used: int | None = None
    storage_free: int | None = None
    storage_mount: str = ""
    disk_active_percent: float | None = None
    disk_read_bps: float | None = None    # bytes per second
    disk_write_bps: float | None = None

    net_rx_bps: float | None = None       # bits per second
    net_tx_bps: float | None = None
    net_rx_total: int | None = None       # bytes since boot
    net_tx_total: int | None = None
    net_interface: str = ""

    @property
    def mem_percent(self) -> float | None:
        if not self.mem_total or self.mem_used is None:
            return None
        return _clamp(100.0 * self.mem_used / self.mem_total)

    @property
    def storage_percent(self) -> float | None:
        """Share occupied, measured the way ``df`` reports Use%."""
        if self.storage_used is None or self.storage_free is None:
            return None
        usable = self.storage_used + self.storage_free
        if usable <= 0:
            return None
        return _clamp(100.0 * self.storage_used / usable)


# ---------------------------------------------------------------- parsing --
def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        if line.startswith("@@"):
            current = line[2:].strip()
            sections.setdefault(current, [])
            continue
        if current is not None and line.strip():
            sections[current].append(line)
    return sections


def _ints(parts) -> list[int]:
    values = []
    for part in parts:
        try:
            values.append(int(part))
        except ValueError:
            break
    return values


def _cpu_counters(values: list[int]) -> tuple[int, int] | None:
    # user nice system idle iowait irq softirq steal; guest time is already
    # inside user, so adding it again would double count it.
    if len(values) < 4:
        return None
    busy_fields = values[:8]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(busy_fields), idle


def _parse_stat(lines, snap: Snapshot) -> None:
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        counters = _cpu_counters(_ints(parts[1:]))
        if counters is None:
            continue
        if parts[0] == "cpu":
            snap.cpu = counters
        elif re.fullmatch(r"cpu\d+", parts[0]):
            snap.cores.append(counters)


def _parse_meminfo(lines, snap: Snapshot) -> None:
    for line in lines:
        name, _sep, rest = line.partition(":")
        values = _ints(rest.split()[:1])
        if name and values:
            snap.meminfo[name.strip()] = values[0]


def _parse_net(lines, snap: Snapshot) -> None:
    for line in lines:
        if ":" not in line or "|" in line:
            continue
        name, _sep, rest = line.partition(":")
        name = name.strip()
        if not name or name.startswith(_VIRTUAL_INTERFACE_PREFIXES):
            continue
        values = _ints(rest.split())
        if len(values) >= 9:
            snap.net[name] = (values[0], values[8])


def _parse_diskstats(lines, snap: Snapshot) -> None:
    for line in lines:
        parts = line.split()
        if len(parts) < 14 or not _WHOLE_DISK.match(parts[2]):
            continue
        stats = _ints(parts[3:])
        if len(stats) >= 10:
            # read sectors, write sectors, milliseconds spent doing I/O
            snap.disks[parts[2]] = (stats[2], stats[6], stats[9])


def _parse_df(lines, snap: Snapshot) -> None:
    # Drop the header, then read from the right so a long filesystem name that
    # wrapped onto its own line cannot shift the columns.
    body = [line for line in lines if not line.lower().startswith("filesystem")]
    tokens = " ".join(body).split()
    if len(tokens) < 5:
        return
    numbers = _ints(tokens[-5:-2])
    if len(numbers) != 3:
        return
    total, used, free = numbers
    snap.storage = (total, used, free, tokens[-1])


def _parse_uptime(lines, snap: Snapshot) -> None:
    if lines:
        match = _LEADING_NUMBER.match(lines[0])
        if match:
            snap.uptime = float(match.group(1))


def _parse_freqs(lines, snap: Snapshot) -> None:
    for line in lines:
        values = _ints(line.split()[:1])
        if values and values[0] > 0:
            snap.freqs_khz.append(values[0])


def _parse_keyed(lines) -> dict[str, str]:
    result = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip():
            result[key.strip()] = value.strip()
    return result


def _parse_temps(lines, snap: Snapshot) -> None:
    for zone, raw in _parse_keyed(lines).items():
        match = _LEADING_NUMBER.match(raw)
        if not match:
            continue
        value = float(match.group(1))
        # Kernels report millidegrees; a handful of drivers report degrees.
        if abs(value) >= 1000:
            value /= 1000.0
        if -40.0 < value < 150.0:
            snap.temps[zone] = max(value, snap.temps.get(zone, value))


def parse_snapshot(text: str) -> Snapshot:
    """Turn one run of ``USAGE_SCRIPT`` into counters.

    Raises ``UsageParseError`` when the reply is not the script's output at all,
    which is what an ADB error such as "device offline" looks like from here.
    """
    sections = _sections(text)
    if "stat" not in sections and "mem" not in sections:
        message = (text or "").strip().splitlines()
        raise UsageParseError(message[0] if message else "No reply from the device")
    snap = Snapshot()
    ids = sections.get("id", [])
    snap.serial = ids[0].strip() if ids else ""
    _parse_stat(sections.get("stat", []), snap)
    _parse_meminfo(sections.get("mem", []), snap)
    _parse_net(sections.get("net", []), snap)
    _parse_diskstats(sections.get("disk", []), snap)
    _parse_df(sections.get("df", []), snap)
    _parse_uptime(sections.get("up", []), snap)
    _parse_freqs(sections.get("freq", []), snap)
    snap.gpu = _parse_keyed(sections.get("gpu", []))
    _parse_temps(sections.get("temp", []), snap)
    if snap.cpu is None and not snap.meminfo:
        raise UsageParseError("The device did not report CPU or memory counters")
    return snap


# ------------------------------------------------------------- arithmetic --
def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _busy_percent(previous: tuple[int, int], current: tuple[int, int]) -> float | None:
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    if total <= 0:
        return None
    return _clamp(100.0 * (total - idle) / total)


def _gpu_reading(gpu: dict[str, str]) -> tuple[float | None, float | None]:
    """Load and clock from whichever driver this device happens to have."""
    load = None
    freq_mhz = None
    for path, raw in gpu.items():
        match = _LEADING_NUMBER.match(raw)
        if not match:
            continue
        value = float(match.group(1))
        if path.endswith("/cur_freq"):
            if value > 0 and freq_mhz is None:
                freq_mhz = value / 1_000_000.0
        elif load is None and 0.0 <= value <= 100.0:
            # devfreq writes "37@800000000Hz": load first, clock after the @.
            load = value
            clock = re.search(r"@(\d+)", raw)
            if clock and freq_mhz is None:
                freq_mhz = int(clock.group(1)) / 1_000_000.0
    return load, freq_mhz


def _hottest(temps: dict[str, float], pattern) -> float | None:
    values = [value for zone, value in temps.items() if pattern.search(zone)]
    return max(values) if values else None


def compute_sample(previous: Snapshot | None, current: Snapshot) -> UsageSample:
    """Combine two snapshots into what the panel shows.

    With no previous snapshot the levels (memory, storage, temperatures) are
    still known; the rates simply stay ``None`` until the next tick.
    """
    sample = UsageSample()
    sample.serial = current.serial
    sample.cpu_cores = len(current.cores)
    sample.uptime_s = current.uptime
    if current.freqs_khz:
        sample.cpu_freq_mhz = sum(current.freqs_khz) / len(current.freqs_khz) / 1000.0
    sample.cpu_temp_c = _hottest(current.temps, _CPU_TEMP_ZONE)
    sample.gpu_temp_c = _hottest(current.temps, _GPU_TEMP_ZONE)
    sample.gpu_percent, sample.gpu_freq_mhz = _gpu_reading(current.gpu)

    mem = current.meminfo
    total = mem.get("MemTotal")
    if total:
        available = mem.get("MemAvailable")
        if available is None:
            # Kernels before 3.14 lack MemAvailable; this is its usual estimate.
            available = mem.get("MemFree", 0) + mem.get("Cached", 0) + mem.get("Buffers", 0)
        available = max(0, min(available, total))
        sample.mem_total = total * 1024
        sample.mem_available = available * 1024
        sample.mem_used = (total - available) * 1024
        if "Cached" in mem:
            sample.mem_cached = mem["Cached"] * 1024

    if current.storage:
        total_kb, used_kb, free_kb, mount = current.storage
        sample.storage_total = total_kb * 1024
        sample.storage_used = used_kb * 1024
        sample.storage_free = free_kb * 1024
        sample.storage_mount = mount

    if current.net:
        busiest = max(current.net, key=lambda name: sum(current.net[name]))
        sample.net_interface = busiest
        sample.net_rx_total = sum(rx for rx, _tx in current.net.values())
        sample.net_tx_total = sum(tx for _rx, tx in current.net.values())

    if previous is None:
        return sample

    if previous.cpu and current.cpu:
        sample.cpu_percent = _busy_percent(previous.cpu, current.cpu)
    if len(previous.cores) == len(current.cores):
        sample.core_percents = [
            _busy_percent(before, after) or 0.0
            for before, after in zip(previous.cores, current.cores)]

    elapsed = None
    if previous.uptime is not None and current.uptime is not None:
        elapsed = current.uptime - previous.uptime
    if not elapsed or elapsed <= 0:
        # A reboot between ticks, or a device that hides /proc/uptime.
        return sample

    shared = set(previous.net) & set(current.net)
    if shared:
        rx = sum(max(0, current.net[n][0] - previous.net[n][0]) for n in shared)
        tx = sum(max(0, current.net[n][1] - previous.net[n][1]) for n in shared)
        sample.net_rx_bps = rx * 8 / elapsed
        sample.net_tx_bps = tx * 8 / elapsed

    disks = set(previous.disks) & set(current.disks)
    if disks:
        reads = sum(max(0, current.disks[d][0] - previous.disks[d][0]) for d in disks)
        writes = sum(max(0, current.disks[d][1] - previous.disks[d][1]) for d in disks)
        busy_ms = max(max(0, current.disks[d][2] - previous.disks[d][2]) for d in disks)
        sample.disk_read_bps = reads * SECTOR_BYTES / elapsed
        sample.disk_write_bps = writes * SECTOR_BYTES / elapsed
        sample.disk_active_percent = _clamp(100.0 * busy_ms / (elapsed * 1000.0))
    return sample


class UsageSampler:
    """Remembers the previous snapshot so each reply becomes one sample."""

    def __init__(self):
        self._previous: Snapshot | None = None

    def reset(self) -> None:
        self._previous = None

    def feed(self, text: str) -> UsageSample:
        current = parse_snapshot(text)
        previous = self._previous
        changed = bool(
            previous is not None and previous.serial and current.serial
            and previous.serial != current.serial)
        if changed:
            # The port now leads to another box; its counters are unrelated.
            previous = None
        # A reboot resets every counter; comparing across it would read as a
        # huge negative rate, so start over instead.
        if (previous is not None and previous.uptime is not None
                and current.uptime is not None and current.uptime < previous.uptime):
            previous = None
        sample = compute_sample(previous, current)
        sample.device_changed = changed
        self._previous = current
        return sample


# ------------------------------------------------------------- formatting --
def format_bytes(value: float | None, digits: int = 1) -> str:
    """Binary units with the labels Windows uses (1 GB = 1024³ bytes)."""
    if value is None:
        return "—"
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.{digits}f} {unit}"
        value /= 1024
    return f"{value:.{digits}f} TB"


def format_bits(value: float | None) -> str:
    """Network speed in bits, as Task Manager shows it."""
    if value is None:
        return "—"
    for unit, scale in (("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
        if value >= scale:
            return f"{value / scale:.1f} {unit}"
    return f"{value:.0f} bps"


def format_byte_rate(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{format_bytes(value)}/s"


def format_percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def format_clock(mhz: float | None) -> str:
    if mhz is None:
        return "—"
    if mhz >= 1000:
        return f"{mhz / 1000:.2f} GHz"
    return f"{mhz:.0f} MHz"


def format_temperature(celsius: float | None) -> str:
    return "—" if celsius is None else f"{celsius:.0f} °C"


def format_uptime(seconds: float | None) -> str:
    """``d:hh:mm:ss`` as Task Manager writes it."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{days}:{hours:02d}:{minutes:02d}:{secs:02d}"


def nice_ceiling(value: float, floor: float) -> float:
    """The next 1-2-5 step at or above ``value``, so graph scales read cleanly."""
    value = max(value, floor)
    magnitude = 1.0
    while magnitude * 10 <= value:
        magnitude *= 10
    while magnitude > value:
        magnitude /= 10
    for step in (1, 2, 5, 10):
        if step * magnitude >= value:
            return step * magnitude
    return 10 * magnitude
