# Convrse Device Control — build and release

Version 2.4.0. Windows x64.

## Quick reference

```powershell
# Full production build: tests, app, smoke check, portable zip, installer
.\build-release.ps1

# Signed release (see "Code signing" below)
.\build-release.ps1 -Sign -CertThumbprint <SHA1-THUMBPRINT>
```

Outputs land in `dist\`:

| Artifact | Path |
| --- | --- |
| Application folder | `dist\ConvrseDeviceControl\` |
| Portable archive | `dist\ConvrseDeviceControl-2.4.0-portable.zip` |
| Installer | `dist\installer\ConvrseDeviceControl-2.4.0-Setup.exe` |

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python 3.11+ | On `PATH` as `python` |
| `requirements.txt` | `python -m pip install -r requirements.txt` |
| `scrcpy-runtime\` | Complete Windows scrcpy distribution, including `scrcpy.exe`, `scrcpy-server`, `adb.exe` |
| Inno Setup 6 | Only needed for the installer. https://jrsoftware.org/isdl.php |
| Windows SDK | Only needed for signing (`signtool.exe`) |

The build fails fast if Python, the dependencies, or the scrcpy runtime are
missing. A missing Inno Setup produces a warning and skips the installer.

---

## Why the packaging changed

### The Defender detection

Builds up to V2.3.4 used PyInstaller's **one-file** mode. That produces a single
executable with the interpreter, PySide6, `adb.exe`, `scrcpy.exe`, and the
FFmpeg and OpenSSL DLLs compressed inside it. On every launch the bootloader
unpacked all of it into `%TEMP%\_MEI<pid>` and executed from there.

Measured on the reference machine:

- **122.9 MB** written to `%TEMP%` per launch
- **287 MB** left abandoned across 11 orphaned `_MEI*` folders
- Files dropped included `adb.exe`, `scrcpy.exe`, `libcrypto-3.dll`,
  `libssl-3.dll`, `python311.dll`

An unsigned executable that writes a hundred-plus megabytes of executables and
crypto libraries into a temporary directory and then runs them is behaviourally
identical to a dropper. Defender's heuristic and ML engines score that pattern,
which is why the detection was generic (`Wacatac`/`Sabsik`-class) rather than a
named signature — nothing in the source was malicious; the *packaging* was the
problem.

**The fix is one-folder mode.** Files are installed to disk once and stay there.
Defender scans them at install time. There is no extraction step, no temporary
copy of `adb.exe`, and no reason for a heuristic to fire.

Nothing about this bypasses, disables, or evades a security control. No
exclusion is added, no scanning is suppressed. The application stops doing the
thing that was being flagged.

### The startup freeze

The startup trace log showed Python-side initialisation completing in ~1.5 s,
which ruled out application code. The freeze was the bootloader: unpacking
123 MB while Defender real-time protection scanned every extracted binary. On a
warm machine that is a couple of seconds; on a cold machine, or the first run
after a download, it is long enough to look hung — with no window on screen,
because the window cannot be created until extraction finishes.

Measured after the change:

| Build | Temp extraction | Time to window |
| --- | --- | --- |
| V2.3.4 one-file | 122.9 MB per launch | 2.6 s warm, much worse cold |
| V2.4.0 one-folder | none | **0.2 s** |

`build-release.ps1` asserts both of these on every build: it fails if a window
does not appear within 60 seconds, and fails if any `_MEI*` directory is
created.

---

## Code signing

Signing and the Defender detection are **separate problems**. The repackaging
above fixes the detection. Signing addresses SmartScreen, which is a
*reputation* prompt, not a malware verdict.

Unsigned, a user downloading the installer may see:

> Windows protected your PC — Unknown publisher

That is SmartScreen reporting that this binary has no reputation yet. It is not
a threat detection, and the wording differs from a Defender detection (which
quarantines the file and names a threat).

### What to buy

| Type | Cost/yr | SmartScreen behaviour |
| --- | --- | --- |
| **OV** (Organisation Validation) | ~$200–400 | Reputation builds over time and downloads; the prompt fades after enough installs |
| **EV** (Extended Validation) | ~$400–700 | Trusted immediately, no reputation period. Requires a hardware token or cloud HSM |

Since June 2023 all publicly trusted code-signing keys must be held on
FIPS-140-2 hardware, so both types now ship on a token or via a cloud signing
service. Issuers: DigiCert, Sectigo, SSL.com, Certera.

For a tool distributed to your own site teams, **OV is sufficient**. EV is worth
it only if you need zero warnings on day one.

### Wiring it in

Once you have the certificate installed in the current user's store:

```powershell
# Find the thumbprint
Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Select-Object Subject, Thumbprint

# Build and sign in one step
.\build-release.ps1 -Sign -CertThumbprint AB12CD34...
```

The script signs with SHA-256 and applies an RFC-3161 timestamp from
`timestamp.digicert.com`. **Timestamping matters**: it keeps already-released
binaries valid after the certificate expires. Without it, everything you have
shipped stops verifying the day the cert lapses.

Both the application executable and the installer are signed.

### Never do these

- Do not create a self-signed certificate and ask users to trust it. It gives
  no reputation, and training people to install root certificates is worse than
  the warning it removes.
- Do not tell users to add a Defender exclusion. An exclusion for the install
  folder disables protection for anything later written there.
- Do not disable real-time protection.

---

## Distributing a release

1. Run `.\build-release.ps1 -Sign -CertThumbprint <thumbprint>` on a clean
   checkout.
2. Confirm the summary reports the window appearing and no temp extraction.
3. Publish `dist\installer\ConvrseDeviceControl-<version>-Setup.exe` as the
   primary download; keep the portable zip as the fallback for machines where
   installing is not permitted.
4. **Keep the filename and certificate stable across releases.** SmartScreen
   reputation accrues per publisher and per binary. Renaming the installer every
   release, as `Convrse-Device-Control-V2.3.1.exe` … `V2.3.4.exe` did, restarts
   the reputation clock each time. The version now lives in the file *metadata*
   and the installer name only, not in the executable name.
5. If a release is ever flagged despite this, submit it to
   https://www.microsoft.com/wdsi/filesubmission as a false positive. With a
   signed, non-self-extracting build these are resolved quickly.

## Where the app keeps things

| What | Where | Notes |
| --- | --- | --- |
| SSH key | `%LOCALAPPDATA%\Convrse\DeviceControl\keys\cdm-key.pem` | Imported once; ACL restricted to the installing account. Survives upgrades and uninstalls. |
| Settings | Registry, `HKCU\Software\Convrse\Convrse Device Control V2.1` | Layout, stream preset, display-protection state |
| Session logs | `Documents\CDC Sessions\` | Opened from File › Open logs folder |
| Startup trace | `Convrse-Device-Control-startup.log` beside the executable | Written only by frozen builds; first stop for a startup complaint |

The leased ADB port is deliberately **not** persisted. See
`cdc_connection.py` for why.

## Verifying a build by hand

```powershell
# No extraction at startup
Get-ChildItem $env:TEMP -Directory -Filter "_MEI*" | Remove-Item -Recurse -Force
Start-Process "dist\ConvrseDeviceControl\Convrse Device Control.exe"
Get-ChildItem $env:TEMP -Directory -Filter "_MEI*"   # must be empty

# Version metadata is present and correct
(Get-Item "dist\ConvrseDeviceControl\Convrse Device Control.exe").VersionInfo

# Signature, if signed
Get-AuthenticodeSignature "dist\ConvrseDeviceControl\Convrse Device Control.exe"

# Ask Defender directly
& "$env:ProgramFiles\Windows Defender\MpCmdRun.exe" -Scan -ScanType 3 `
    -File (Resolve-Path "dist\ConvrseDeviceControl").Path
```
