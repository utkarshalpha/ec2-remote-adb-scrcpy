# EC2 Remote ADB & scrcpy

**Convrse Device Control (CDC)** is a native PySide6/Qt operations console for
Android devices reached through an EC2/SSH tunnel. It combines remote ADB,
low-latency scrcpy mirroring, media-box control, app recovery, rooted display
protection, and production diagnostics.

## Interface

- Mirror-first native Qt workspace that opens maximized and remains usable down
  to 820×500.
- Aspect-fit mirroring with no cropping; Full screen, F11, and mirror
  double-click enter focus mode, while Escape exits it.
- Fixed, consistent control sizing: 40 px connection actions, 36 px sidebar
  actions, and 38 px app-bar controls.
- Two compact sidebar categories: Remote and Device.
- The menu bar stays out of the workspace until Alt is pressed. The command log
  is a thin collapsed strip until Ctrl+J is pressed.

## Connection

1. Select the local `.pem` key.
2. Enter a shared port such as `17000` or `17003`.
3. Click **Connect**.
4. CDC automatically establishes and verifies the rooted Display Guard.
5. Click **Start mirror** after protection and stream detection complete.

CDC creates `-L PORT:localhost:PORT` to `ubuntu@cdm.convrse.ai`, then connects
ADB to `127.0.0.1:PORT`. Windows OpenSSH Client is required.

## Production features

- Five reviewed H.264 profiles: Low 640×360/20 FPS/0.75 Mbps, Balanced
  960×540/25 FPS/1.5 Mbps, Normal 1280×720/30 FPS/5 Mbps, Native
  30 FPS/8 Mbps, and Max native/up to 60 FPS/12 Mbps.
- A hidden-until-selected Custom profile with resolution, FPS, and 0.75–12
  Mbps controls. Editing fields never restarts the mirror; Apply performs one
  restart.
- Android playback audio is routed to the PC with Opus at 128 kbps and a 200 ms
  buffer. CDC never selects a microphone source.
- Per-device stream capability detection and verified hardware H.264 selection,
  with a one-time automatic-encoder fallback when a hardware encoder fails.
- Media-box remote controls with unsupported phone-only buttons removed.
- Direct APK install/update, Convrse Store, CleanUp, and Claude launch.
- Automatic foreground-package detection.
- Force stop, restart, cache-only clearing, confirmed data clearing, and cached
  background-process cleanup.
- Rockchip RK3576 AI-PQ guard covering DC, feature extraction, AI scene
  detection, local contrast, sharpening, IPTV/SR and MEMC properties.
- Display Guard gates mirroring until root identity, runtime properties, rooted
  Settings preferences, and a stability read-back are verified. Manual drift is
  corrected automatically; failures identify offline, non-rooted, unsupported,
  denied, ignored, or reverted states instead of showing a false green result.
- Automatic 30-minute diagnostic sessions after ADB connects.
- Continuous logcat, foreground transitions, PID/memory/network metrics, raw
  input events, crash markers, final screenshot, and a local diagnostic ZIP.

Session ZIPs are saved under `Documents\CDC Sessions`.

## Repository layout

- Current Windows and Apple Silicon sources stay at the repository root.
- Automated tests are under `tests`; operational references are under `docs`.
- Historical executables are preserved locally under `versions` and excluded
  from Git so the source repository remains small.
- Verified scrcpy runtimes are local build inputs and are not committed.
- Private keys, signing identities, device logs, diagnostic ZIPs, caches, and
  virtual environments are always excluded.

## Safety

- Clear Data always shows the detected package and requires confirmation.
- Android System UI, Settings, and the active launcher are protected.
- Cache-only clearing uses `run-as`; unsupported release apps fall back to the
  Android App Info screen instead of deleting data.
- AI properties are read first, enforced with paced rooted writes, synchronized
  with the vendor Settings cache, and independently verified again.

## Build

V2.3 is a self-contained, versioned build. Before packaging, create a
`scrcpy-runtime` directory at the project root and place the complete Windows
scrcpy distribution in it. At minimum it must contain:

```text
scrcpy-runtime/
  scrcpy.exe
  scrcpy-server
  adb.exe
  AdbWinApi.dll
  AdbWinUsbApi.dll
  ...all DLLs shipped with that scrcpy distribution
```

Keep `scrcpy.exe`, `scrcpy-server`, and the native DLLs from the same release;
mixing versions can make startup or audio/video negotiation fail. The V2.3
build must use one complete, matching scrcpy release.

Run the versioned build script:

```powershell
.\build-v2.3.bat
```

The script validates the runtime, installs dependencies, runs the automated
tests, and writes:

```text
versions\windows\v2.3\Convrse-Device-Control-V2.3.exe
```

V2.3 uses its own spec, work directory, output directory, executable name, and
Windows version metadata. It does not overwrite the V2.2 executable or build.

In a frozen one-file build, CDC resolves tools in this order:

1. The bundled `sys._MEIPASS\scrcpy-runtime` directory.
2. The root of `sys._MEIPASS` for compatibility with older packages.
3. An external `scrcpy-runtime` directory beside the installed executable.
4. A tool placed directly beside the executable.
5. The system `PATH`.

For an unpackaged development run, an external `scrcpy-runtime` directory is
preferred, but placing the tools beside the script or on `PATH` remains
supported.

For development, run `python cdc_v2.py`. The previous Tk interface remains in
`scrcpy_remote.py` as a compatibility fallback; the packaged and recommended
interface is `cdc_v2.py`.

## Apple Silicon macOS edition

The separate macOS 2.4 package preserves Windows V2.3 and targets M-series Macs
only. It bundles the verified official scrcpy 4.1 Apple Silicon runtime, ADB,
audio support, CDC profiles, SSH handling, diagnostics, and Display Guard.
End users open the DMG, drag **Convrse Device Control** to Applications, and
double-click it; Python, Homebrew, and Terminal are not required.

Build it once on an M-series Mac by double-clicking
`Build-Mac-Apple-Silicon.command` or running:

```sh
bash build-macos-arm64.sh
```

The resulting installer is written to `versions/macos/v2.4.0`. The control app and
scrcpy mirror use separate native Mac windows because the Windows HWND
embedding mechanism has no safe Cocoa equivalent. See
`docs/MACOS-APPLE-SILICON.md` for signing, notarization, and distribution details.
