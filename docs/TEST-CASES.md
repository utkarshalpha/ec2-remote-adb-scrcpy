# Session Tracking Test Cases — 30-Minute Terminal Session

**Device under test:** H96 Max M9 (Rockchip RK3576), Android 14, userdebug, adb over SSH tunnel (`127.0.0.1:17000`).
**Scope:** One full 30-minute session on a deployed terminal — from session start to session end — tracking input (touches / remote interactions), app stability, and device health.
**How to run:** Every test case lists the exact adb command. Replace `$S` with the device serial (`127.0.0.1:17000`). Sample at the stated interval, record values in the *Record* column of the session sheet.

> Known device fact (found 2026-07-24): with the touch panel disconnected, the box enumerates **no touchscreen** — only the "XFY MIC RC Mouse/Keyboard" remote, power/GPIO keys, and HDMI-CEC. "User input" on this hardware therefore means: touchscreen events (when panel present) + remote mouse clicks + remote key presses. scrcpy-injected input does NOT appear in `getevent` (it is injected above the driver layer) — physical input only.

---

## A. Session start (minute 0)

### TC-SES-01 — Session baseline snapshot
- **Objective:** Record the starting state so every later measurement has a reference.
- **How:**
  `adb -s $S shell "date; uptime; getprop ro.build.version.release; dumpsys activity activities | grep -E 'ResumedActivity'"`
  `adb -s $S shell dumpsys meminfo -s <package>`
- **Record:** timestamp, device uptime, foreground package/activity, app PSS (MB), free RAM.
- **Pass:** expected demo app is foreground; device uptime plausible (no unexplained reboot since last session).

### TC-SES-02 — Input hardware present
- **Objective:** Confirm the touch panel is actually connected before counting anything.
- **How:** `adb -s $S shell getevent -p` — look for a device advertising `ABS_MT_POSITION_X` / `BTN_TOUCH`.
- **Record:** device node (e.g. `/dev/input/event8`), device name; if absent, note "remote-only session".
- **Pass:** touch device present **if the terminal is a touch installation**; otherwise remote (mouse/keyboard) devices present.
- **Fail action:** no input device at all → panel unplugged / USB fault → hardware ticket, session continues as observation-only.

### TC-SES-03 — Start the 30-min recorders
- **Objective:** Everything below is measured from these two background streams.
- **How:**
  Log stream: `adb -s $S logcat -T 1 -v threadtime -b main -b system -b crash > session-full.log`
  Input stream: `adb -s $S shell getevent -lt > session-input.log` (all devices, timestamped)
- **Record:** start timestamps of both files.
- **Pass:** both streams producing output within 10 s.

---

## B. Input tracking (continuous, report per 5-minute bucket)

### TC-TCH-04 — Total touch count
- **Objective:** How many physical touches the device received in the session.
- **How:** From `session-input.log`, count touch DOWNs on the touch device:
  count lines `BTN_TOUCH DOWN`; if the panel is type-B multitouch (no BTN_TOUCH), count `ABS_MT_TRACKING_ID` transitions from `ffffffff` to any value.
  Quick live check: `adb -s $S shell "getevent -lc 200 /dev/input/eventX"` while someone uses the screen.
- **Record:** total touch count; touches per 5-min bucket (0–5, 5–10, … 25–30).
- **Pass:** count > 0 in an attended demo session; buckets roughly match observed usage.

### TC-TCH-05 — Remote/mouse interaction count
- **Objective:** Same as TC-TCH-04 for terminals driven by the RC remote.
- **How:** From `session-input.log`, count `BTN_LEFT DOWN` (remote mouse clicks) and `KEY_* DOWN` (remote key presses) on the XFY RC devices.
- **Record:** clicks + key presses per bucket.
- **Pass:** interaction count > 0 when session is attended.

### TC-TCH-06 — Ghost / stuck touch detection
- **Objective:** Catch faulty panels that fire phantom input or hold a touch down.
- **How:** From `session-input.log`:
  ghost = burst of >15 touch DOWNs within 1 s with no matching human presence;
  stuck = `BTN_TOUCH DOWN` (or live tracking ID) with no UP for >5 s.
- **Record:** number of ghost bursts, number of stuck-touch incidents, timestamps.
- **Pass:** zero of both.

### TC-TCH-07 — Touch-to-response check (spot check, once per session)
- **Objective:** Confirm the app visibly reacts to input without noticeable lag.
- **How:** While watching via scrcpy, perform (or ask on-site staff to perform) 5 taps on interactive elements; after, run `adb -s $S shell dumpsys gfxinfo <package>` and note frame stats around those moments.
- **Record:** subjective response (immediate / delayed / missed), janky-frame % from gfxinfo.
- **Pass:** all 5 taps produce a visible reaction; no tap "swallowed".

---

## C. App stability (poll every 5 minutes)

### TC-APP-08 — Foreground retention
- **Objective:** The demo app must stay in front for the whole session (kiosk guarantee).
- **How:** `adb -s $S shell "dumpsys activity activities | grep ResumedActivity"` at each 5-min mark.
- **Record:** foreground package at each mark; any period where launcher/other app was in front.
- **Pass:** expected package foreground at 7/7 checkpoints (0,5,10,15,20,25,30).

### TC-APP-09 — Zero crashes / ANRs during session
- **Objective:** No fatal exceptions, native crashes, or ANRs in the 30 minutes.
- **How:** After session: `grep -E "FATAL EXCEPTION|ANR in|am_crash|Fatal signal" session-full.log`
  Also diff dropbox: `adb -s $S shell dumpsys dropbox --print | grep -E "crash|anr"` (entries newer than session start)
  Also tombstones: `adb -s $S shell ls -lt /data/tombstones` (userdebug allows read).
- **Record:** each incident: time, process, first exception line.
- **Pass:** zero incidents. Any incident → pull full report and add to the Crash Findings Register below.

### TC-APP-10 — Memory growth (leak watch)
- **Objective:** App memory must not climb unbounded across the session.
- **How:** `adb -s $S shell dumpsys meminfo -s <package>` at minutes 0, 10, 20, 30.
- **Record:** TOTAL PSS at each mark.
- **Pass:** PSS at minute 30 ≤ 1.3 × PSS at minute 0, and no steady monotonic climb (Unity apps fluctuate; a straight upward line is the red flag).

### TC-APP-11 — App restarts / PID stability
- **Objective:** Detect silent restarts (crash-loop hidden by fast auto-relaunch).
- **How:** `adb -s $S shell pidof <package>` at each 5-min mark.
- **Record:** PID at each mark.
- **Pass:** same PID at all 7 checkpoints (a changed PID = the app died and restarted → investigate with TC-APP-09).

---

## D. Device health (poll every 10 minutes)

### TC-SYS-12 — CPU load
- **How:** `adb -s $S shell "top -n 1 -b | head -15"`
- **Record:** load average, top process %CPU.
- **Pass:** sustained load average < number of cores (8); demo app not pinned at 100% of all cores.

### TC-SYS-13 — Thermals
- **How:** `adb -s $S shell "cat /sys/class/thermal/thermal_zone*/type /sys/class/thermal/thermal_zone*/temp"`
- **Record:** SoC temperature at 0/10/20/30 min.
- **Pass:** < 85 °C throughout; no throttle-then-jank correlation with TC-TCH-07.

### TC-SYS-14 — Storage headroom
- **How:** `adb -s $S shell df -h /data`
- **Record:** free space on /data.
- **Pass:** > 1 GB free (logs, updates, and Unity caches need room).

### TC-NET-15 — Link stability
- **Objective:** The SSH/adb path to the box must survive the whole session.
- **How:** `adb -s $S shell echo alive` at each 5-min mark; note any "device offline" and reconnect time.
- **Record:** checkpoint results; count of drops.
- **Pass:** 0 drops (note: one drop was observed on 2026-07-24 — track this as a recurring metric).

---

## E. Session end (minute 30)

### TC-END-16 — Artifact bundle saved
- **Objective:** Every session leaves a reviewable evidence bundle.
- **How:** Stop both recorders; collect into one ZIP:
  `session-full.log`, `session-input.log`, the filled session sheet (counts + checkpoint table), final screenshot (`adb -s $S exec-out screencap -p > end.png`), `dumpsys meminfo` final, `dumpsys dropbox --print` excerpt.
- **Record:** ZIP filename `session-<device>-<date>-<start-time>.zip`.
- **Pass:** ZIP exists and contains all six items.

### TC-END-17 — Session summary line
- **Objective:** One line per session for trend tracking across days/devices.
- **Format:** `date | device | duration | touches | clicks/keys | crashes | anrs | fg-retention % | max-temp | drops`
- **Pass:** line appended to the master session log (spreadsheet or CSV).

---

## F. Crash Findings Register (to be filled from the last 10–15 crash reports)

> Paste/share each crash report; each confirmed root cause becomes a regression test case appended in section G.

| # | Date | App/package | Crash signature (first line of exception) | Root cause found | Regression TC |
|---|------|-------------|--------------------------------------------|------------------|---------------|
| 1 | | | | | TC-REG-xx |
| 2 | | | | | |
| … | | | | | |

## G. Regression test cases (generated from Section F)

*(Empty until the crash reports are reviewed — each entry will follow the same Objective / How / Record / Pass format, reproducing the trigger condition of a fixed crash and asserting it no longer occurs.)*
