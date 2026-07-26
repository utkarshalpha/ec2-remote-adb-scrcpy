# Convrse Device Control for Apple Silicon

This package produces a normal Mac application and DMG. End users do not need
Python, Homebrew, ADB, scrcpy, or Terminal.

## End-user installation

1. Open `Convrse-Device-Control-2.4.0-Apple-Silicon.dmg`.
2. Drag **Convrse Device Control** to **Applications**.
3. Double-click **Convrse Device Control**.

ADB, scrcpy 4.1, the scrcpy server, Android audio routing, the CDC stream
profiles, SSH connection handling, diagnostics, and rooted Display Guard are
inside the app bundle.

## Build once on an M-series Mac

Copy this source folder to the Mac and double-click
`Build-Mac-Apple-Silicon.command`. If macOS does not preserve its executable
bit, open Terminal in the folder once and run:

```sh
bash build-macos-arm64.sh
```

The builder verifies the official scrcpy 4.1 archive SHA-256, creates the Mac
icon from the supplied Convrse logo, runs the complete tests, builds the `.app`,
and creates the drag-to-Applications DMG under `versions/macos/v2.4.0`.

## Native mirror behavior

The CDC controls stay in the main application and the Android display opens in
scrcpy's own native Mac window. This preserves low latency, hardware decoding,
device-to-Mac audio, keyboard/mouse control, full screen, and drag-and-drop.
The Windows-only HWND re-parenting technique cannot safely embed another
process's Cocoa window on macOS.

## Public distribution

An unsigned/ad-hoc build is suitable for testing on the Mac that built it. For
frictionless installation on other Macs, provide an Apple Developer ID
Application certificate through `CDC_CODESIGN_IDENTITY`, and a saved
`notarytool` keychain profile through `CDC_NOTARY_PROFILE`. The build script
then submits and staples the DMG. The signing credentials are never stored in
this project.
