"""Fake replies to cdc_usage.USAGE_SCRIPT, shaped like a real RK3576 box."""

import math


class FakeDevice:
    """Counters that advance like a real device between ticks."""

    def __init__(self, cores=8, mem_total_kb=4_011_000, uptime=11_000.0,
                 gpu=True, storage=(25_165_824, 15_000_000, 10_165_824),
                 serial="SN2026020201944"):
        self.serial = serial
        self.cores = cores
        self.mem_total_kb = mem_total_kb
        self.uptime = uptime
        self.gpu = gpu
        self.storage = storage
        self.tick = 0
        self.cpu = [[1000, 0, 500, 100_000, 200, 0, 10, 0] for _ in range(cores)]
        self.rx = 900_000_000
        self.tx = 40_000_000
        self.rsect = 2_000_000
        self.wsect = 900_000
        self.io_ms = 500_000

    def advance(self, seconds=2.0, busy=0.35, rx_bps=4e6, tx_bps=3e5,
                io_fraction=0.05):
        self.tick += 1
        self.uptime += seconds
        jiffies = int(seconds * 100)
        for index, core in enumerate(self.cpu):
            share = max(0.0, min(1.0, busy + 0.25 * math.sin(self.tick / 3 + index)))
            used = int(jiffies * share)
            core[0] += used
            core[3] += jiffies - used
        self.rx += int(rx_bps * seconds / 8)
        self.tx += int(tx_bps * seconds / 8)
        self.rsect += int(3_000_000 * seconds / 512)
        self.wsect += int(800_000 * seconds / 512)
        self.io_ms += int(seconds * 1000 * io_fraction)

    def reply(self, mem_available_kb=1_900_000):
        total = [sum(column) for column in zip(*self.cpu)]
        lines = ["@@id", self.serial, "@@stat", "cpu  " + " ".join(map(str, total)) + " 0 0"]
        for index, core in enumerate(self.cpu):
            lines.append(f"cpu{index} " + " ".join(map(str, core)) + " 0 0")
        lines += ["intr 12345 0 0", "@@mem",
                  f"MemTotal:        {self.mem_total_kb} kB",
                  "MemFree:          300000 kB",
                  f"MemAvailable:    {mem_available_kb} kB",
                  "Buffers:           20000 kB",
                  "Cached:          1200000 kB",
                  "@@net",
                  "Inter-|   Receive                                                |  Transmit",
                  " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed",
                  "    lo:  123456     100    0    0    0     0          0         0   123456     100    0    0    0     0       0          0",
                  f"  eth0: {self.rx} 900000 0 0 0 0 0 0 {self.tx} 400000 0 0 0 0 0 0",
                  "  wlan0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0",
                  "@@disk",
                  f" 179       0 mmcblk0 50000 100 {self.rsect} 9000 30000 200 {self.wsect} 8000 0 {self.io_ms} 17000 0 0 0 0",
                  f" 179       1 mmcblk0p1 10 0 80 1 0 0 0 0 0 5 1 0 0 0 0",
                  f" 179      32 mmcblk0boot0 10 0 80 1 0 0 0 0 0 5 1 0 0 0 0",
                  "   7       0 loop0 10 0 80 1 0 0 0 0 0 5 1 0 0 0 0",
                  "@@df",
                  "Filesystem      1K-blocks     Used Available Use% Mounted on",
                  f"/dev/block/dm-6 {self.storage[0]} {self.storage[1]} {self.storage[2]}  60% /data",
                  "@@up", f"{self.uptime:.2f} 80000.00",
                  "@@freq"]
        lines += [str(1_416_000 if i < 4 else 2_208_000) for i in range(self.cores)]
        lines.append("@@gpu")
        if self.gpu:
            load = int(30 + 20 * math.sin(self.tick / 4))
            lines += [f"/sys/class/devfreq/27800000.gpu/load={load}@800000000Hz",
                      "/sys/class/devfreq/27800000.gpu/cur_freq=800000000"]
        lines += ["@@temp", "soc-thermal=51800", "bigcore0-thermal=53100",
                  "gpu-thermal=49900", "npu-thermal=48000", "@@end"]
        return "\n".join(lines) + "\n"
