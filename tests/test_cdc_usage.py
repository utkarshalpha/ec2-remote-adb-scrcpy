import os
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for path in (PROJECT_ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import cdc_usage as usage
from usage_fixtures import FakeDevice


class ParseSnapshotTests(unittest.TestCase):
    def test_reads_every_section_of_a_real_shaped_reply(self):
        snap = usage.parse_snapshot(FakeDevice().reply())
        self.assertIsNotNone(snap.cpu)
        self.assertEqual(len(snap.cores), 8)
        self.assertEqual(snap.meminfo["MemTotal"], 4_011_000)
        self.assertEqual(set(snap.net), {"eth0", "wlan0"})     # lo dropped
        self.assertEqual(set(snap.disks), {"mmcblk0"})          # whole disk only
        self.assertEqual(snap.storage[3], "/data")
        self.assertAlmostEqual(snap.uptime, 11_000.0)
        self.assertEqual(len(snap.freqs_khz), 8)
        self.assertEqual(snap.temps["soc-thermal"], 51.8)

    def test_crlf_reply_parses_the_same(self):
        text = FakeDevice().reply()
        self.assertEqual(
            usage.parse_snapshot(text.replace("\n", "\r\n")),
            usage.parse_snapshot(text))

    def test_adb_error_is_a_parse_error_with_its_message(self):
        for reply in ("error: device offline", "", "error: no devices/emulators found"):
            with self.assertRaises(usage.UsageParseError) as caught:
                usage.parse_snapshot(reply)
            if reply:
                self.assertIn(reply, str(caught.exception))

    def test_markers_without_counters_are_rejected(self):
        with self.assertRaises(usage.UsageParseError):
            usage.parse_snapshot("@@stat\n@@mem\n@@end\n")

    def test_wrapped_df_line_still_reads_from_the_right(self):
        reply = FakeDevice().reply().replace(
            "/dev/block/dm-6 25165824",
            "/dev/block/bootdevice/by-name/userdata\n   25165824")
        snap = usage.parse_snapshot(reply)
        self.assertEqual(snap.storage, (25_165_824, 15_000_000, 10_165_824, "/data"))

    def test_temperatures_in_degrees_and_out_of_range_values(self):
        reply = FakeDevice().reply().replace(
            "npu-thermal=48000", "npu-thermal=47\nbogus-thermal=-273000\nempty=")
        snap = usage.parse_snapshot(reply)
        self.assertEqual(snap.temps["npu-thermal"], 47.0)
        self.assertNotIn("bogus-thermal", snap.temps)
        self.assertNotIn("empty", snap.temps)

    def test_virtual_interfaces_are_not_counted(self):
        reply = FakeDevice().reply().replace(
            "  wlan0:", "  dummy0: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0\n  sit0: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0\n  wlan0:")
        self.assertEqual(set(usage.parse_snapshot(reply).net), {"eth0", "wlan0"})


class ComputeSampleTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeDevice()
        self.sampler = usage.UsageSampler()

    def _tick(self, **kwargs):
        self.device.advance(**kwargs)
        return self.sampler.feed(self.device.reply())

    def test_first_sample_has_levels_but_no_rates(self):
        sample = self.sampler.feed(self.device.reply())
        self.assertIsNone(sample.cpu_percent)
        self.assertIsNone(sample.net_rx_bps)
        self.assertIsNone(sample.disk_active_percent)
        self.assertEqual(sample.mem_total, 4_011_000 * 1024)
        self.assertIsNotNone(sample.storage_percent)
        self.assertEqual(sample.cpu_cores, 8)

    def test_cpu_percent_matches_the_busy_share(self):
        self.sampler.feed(self.device.reply())
        # The fixture adds a per-core sine wave; with busy=1.0 every core is
        # pinned at 100 regardless of it.
        sample = self._tick(busy=2.0)
        self.assertAlmostEqual(sample.cpu_percent, 100.0, places=3)
        sample = self._tick(busy=-2.0)
        self.assertAlmostEqual(sample.cpu_percent, 0.0, places=3)
        self.assertEqual(len(sample.core_percents), 8)

    def test_network_rate_uses_device_uptime_not_host_time(self):
        self.sampler.feed(self.device.reply())
        sample = self._tick(seconds=4.0, rx_bps=8e6, tx_bps=1e6)
        self.assertAlmostEqual(sample.net_rx_bps, 8e6, delta=8e6 * 0.001)
        self.assertAlmostEqual(sample.net_tx_bps, 1e6, delta=1e6 * 0.001)
        self.assertEqual(sample.net_interface, "eth0")

    def test_disk_active_time_and_throughput(self):
        self.sampler.feed(self.device.reply())
        sample = self._tick(seconds=2.0, io_fraction=0.25)
        self.assertAlmostEqual(sample.disk_active_percent, 25.0, places=3)
        self.assertAlmostEqual(sample.disk_read_bps, 3_000_000, delta=600)
        self.assertAlmostEqual(sample.disk_write_bps, 800_000, delta=600)

    def test_disk_active_time_is_capped(self):
        self.sampler.feed(self.device.reply())
        self.assertEqual(self._tick(io_fraction=3.0).disk_active_percent, 100.0)

    def test_memory_in_use_and_available_add_up_to_total(self):
        sample = self.sampler.feed(self.device.reply(mem_available_kb=1_000_000))
        self.assertEqual(sample.mem_used + sample.mem_available, sample.mem_total)
        self.assertAlmostEqual(sample.mem_percent, 100 * 3_011_000 / 4_011_000)

    def test_memory_without_memavailable_uses_the_classic_estimate(self):
        reply = "\n".join(
            line for line in self.device.reply().splitlines()
            if not line.startswith("MemAvailable"))
        sample = self.sampler.feed(reply)
        self.assertEqual(sample.mem_available, (300_000 + 1_200_000 + 20_000) * 1024)

    def test_storage_used_and_free_match_df(self):
        sample = self.sampler.feed(self.device.reply())
        self.assertEqual(sample.storage_used, 15_000_000 * 1024)
        self.assertEqual(sample.storage_free, 10_165_824 * 1024)
        self.assertAlmostEqual(
            sample.storage_percent, 100 * 15_000_000 / 25_165_824, places=3)
        self.assertEqual(sample.storage_mount, "/data")

    def test_gpu_from_devfreq_load_and_clock(self):
        sample = self.sampler.feed(self.device.reply())
        self.assertIsNotNone(sample.gpu_percent)
        self.assertAlmostEqual(sample.gpu_freq_mhz, 800.0)
        self.assertEqual(sample.gpu_temp_c, 49.9)
        self.assertEqual(sample.cpu_temp_c, 53.1)

    def test_gpu_from_adreno_kgsl(self):
        reply = FakeDevice(gpu=False).reply().replace(
            "@@temp", "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage=23 %\n@@temp")
        sample = usage.UsageSampler().feed(reply)
        self.assertEqual(sample.gpu_percent, 23.0)

    def test_gpu_missing_is_none_not_zero(self):
        sample = usage.UsageSampler().feed(FakeDevice(gpu=False).reply())
        self.assertIsNone(sample.gpu_percent)
        self.assertIsNone(sample.gpu_freq_mhz)

    def test_reboot_between_ticks_starts_over_instead_of_going_negative(self):
        self.sampler.feed(self.device.reply())
        rebooted = FakeDevice(uptime=30.0)
        sample = self.sampler.feed(rebooted.reply())
        self.assertIsNone(sample.cpu_percent)
        self.assertIsNone(sample.net_rx_bps)
        rebooted.advance(rx_bps=1e6)
        sample = self.sampler.feed(rebooted.reply())
        self.assertGreater(sample.net_rx_bps, 0)

    def test_counter_going_backwards_never_reports_negative(self):
        self.sampler.feed(self.device.reply())
        self.device.advance()
        self.device.rx -= 10_000_000      # an interface reset its counters
        sample = self.sampler.feed(self.device.reply())
        self.assertGreaterEqual(sample.net_rx_bps, 0)

    def test_reset_forgets_the_previous_device(self):
        self.sampler.feed(self.device.reply())
        self.sampler.reset()
        self.device.advance()
        self.assertIsNone(self.sampler.feed(self.device.reply()).cpu_percent)

    def test_long_run_stays_within_bounds(self):
        self.sampler.feed(self.device.reply())
        for index in range(200):
            sample = self._tick(busy=(index % 7) / 6, io_fraction=(index % 5) / 4)
            self.assertTrue(0.0 <= sample.cpu_percent <= 100.0)
            self.assertTrue(all(0.0 <= v <= 100.0 for v in sample.core_percents))
            self.assertTrue(0.0 <= sample.disk_active_percent <= 100.0)
            self.assertTrue(0.0 <= sample.mem_percent <= 100.0)


class FormattingTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(usage.format_bytes(None), "—")
        self.assertEqual(usage.format_bytes(512), "512 B")
        self.assertEqual(usage.format_bytes(1536), "1.5 KB")
        self.assertEqual(usage.format_bytes(4_011_000 * 1024), "3.8 GB")
        self.assertEqual(usage.format_bytes(5 * 1024 ** 4), "5.0 TB")

    def test_bits_and_rates(self):
        self.assertEqual(usage.format_bits(None), "—")
        self.assertEqual(usage.format_bits(0), "0 bps")
        self.assertEqual(usage.format_bits(272_900), "272.9 Kbps")
        self.assertEqual(usage.format_bits(4_100_000), "4.1 Mbps")
        self.assertEqual(usage.format_byte_rate(3 * 1024 * 1024), "3.0 MB/s")

    def test_clock_temperature_uptime_percent(self):
        self.assertEqual(usage.format_clock(800), "800 MHz")
        self.assertEqual(usage.format_clock(1812), "1.81 GHz")
        self.assertEqual(usage.format_temperature(51.8), "52 °C")
        self.assertEqual(usage.format_uptime(3 * 86400 + 3725), "3:01:02:05")
        self.assertEqual(usage.format_percent(27.4), "27%")
        self.assertEqual(usage.format_percent(None), "—")

    def test_nice_ceiling_steps(self):
        self.assertEqual(usage.nice_ceiling(0, 100_000), 100_000)
        self.assertEqual(usage.nice_ceiling(130_000, 100_000), 200_000)
        self.assertEqual(usage.nice_ceiling(4_100_000, 100_000), 5_000_000)
        self.assertEqual(usage.nice_ceiling(5_000_000, 100_000), 5_000_000)
        self.assertEqual(usage.nice_ceiling(6_000_000, 100_000), 10_000_000)


class ScriptTests(unittest.TestCase):
    def test_script_is_one_line_with_every_section(self):
        self.assertNotIn("\n", usage.USAGE_SCRIPT)
        for marker in ("@@stat", "@@mem", "@@net", "@@disk", "@@df", "@@up",
                       "@@freq", "@@gpu", "@@temp", "@@end"):
            self.assertIn(f"echo {marker}", usage.USAGE_SCRIPT)
        # stderr must be silenced so a missing sysfs file cannot break a section.
        self.assertTrue(usage.USAGE_SCRIPT.startswith("exec 2>/dev/null;"))


if __name__ == "__main__":
    unittest.main()
