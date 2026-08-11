<#
.SYNOPSIS
    Reproducible production build for Convrse Device Control.

.DESCRIPTION
    Runs the test suite, produces a one-folder application, and optionally
    signs it and wraps it in an installer.

    The one-folder layout is deliberate. The previous one-file build unpacked
    ~123 MB into %TEMP% on every launch and ran adb.exe, scrcpy.exe and the
    OpenSSL DLLs from there, which is what Windows Defender was scoring as a
    threat and what made a cold start appear to hang. Nothing here disables or
    evades a security control; the application simply no longer behaves like
    something that warrants flagging.

.PARAMETER Sign
    Sign the executable and installer with a real code-signing certificate.
    Requires -CertThumbprint, or a SIGN_THUMBPRINT environment variable.
    Signing is what removes the SmartScreen "unknown publisher" prompt; it is
    a separate concern from the Defender detection, which the packaging fixes.

.PARAMETER CertThumbprint
    SHA-1 thumbprint of a certificate in the current user's store.

.PARAMETER SkipTests
    Skip the test suite. Intended for local iteration only.

.EXAMPLE
    .\build-release.ps1
    .\build-release.ps1 -Sign -CertThumbprint AB12...CD
#>
[CmdletBinding()]
param(
    [switch]$Sign,
    [string]$CertThumbprint = $env:SIGN_THUMBPRINT,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$AppVersion  = '2.4.0'
$DistName    = 'ConvrseDeviceControl'
$ExeName     = 'Convrse Device Control.exe'
$DistDir     = Join-Path $PSScriptRoot "dist\$DistName"
$ExePath     = Join-Path $DistDir $ExeName
$TimestampUrl = 'http://timestamp.digicert.com'

function Write-Step { param([string]$Text) Write-Host "`n== $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "   $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "   $Text" -ForegroundColor Yellow }

# ---------------------------------------------------------------- checks --
Write-Step "Checking the build environment"
python --version
if ($LASTEXITCODE -ne 0) { throw "Python is not on PATH." }

python -c "import PySide6, PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warn "Installing packaging dependencies from requirements.txt"
    python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency install failed." }
}

foreach ($required in @('scrcpy.exe', 'scrcpy-server', 'adb.exe')) {
    $path = Join-Path $PSScriptRoot "scrcpy-runtime\$required"
    if (-not (Test-Path $path)) {
        throw "The scrcpy runtime is incomplete: scrcpy-runtime\$required is missing."
    }
}
Write-Ok "Python, PySide6, PyInstaller and the scrcpy runtime are present."

# ------------------------------------------------------------------ tests --
if ($SkipTests) {
    Write-Warn "Tests skipped by request. Do not ship a build made this way."
} else {
    Write-Step "Running the test suite"
    $env:PYTHONIOENCODING = 'utf-8'
    python -m unittest discover -s tests -p "test_*.py"
    if ($LASTEXITCODE -ne 0) { throw "Tests failed. The build stops here." }
    Write-Ok "All tests passed."
}

# ------------------------------------------------------------------ build --
Write-Step "Building the one-folder application"
# A running instance -- often the previous run's smoke check -- holds a lock on
# the executable and makes PyInstaller fail with an opaque access-denied error.
$running = Get-Process -Name 'Convrse Device Control' -ErrorAction SilentlyContinue
if ($running) {
    Write-Warn "Closing $($running.Count) running instance(s) first."
    $running | Stop-Process -Force
    Start-Sleep -Seconds 1
}
Remove-Item -Recurse -Force $DistDir, "$PSScriptRoot\build-release" -ErrorAction SilentlyContinue

# Windows keeps a handle on the output folder for a while after the app runs --
# Defender, the search indexer and Explorer preview all do it -- and PyInstaller
# then fails with an opaque access-denied. Stage into a fresh folder instead of
# demanding the machine let go of the old one.
if (Test-Path $DistDir) {
    $stamp = (Get-Date -Format 'yyyyMMdd-HHmmss')
    $DistName = "ConvrseDeviceControl-$stamp"
    $DistDir  = Join-Path $PSScriptRoot "dist\$DistName"
    $ExePath  = Join-Path $DistDir $ExeName
    Write-Warn "Previous output folder is locked; staging into $DistName instead."
}
$env:CDC_DIST_NAME = $DistName
python -m PyInstaller --noconfirm --clean `
    --distpath "$PSScriptRoot\dist" `
    --workpath "$PSScriptRoot\build-release" `
    convrse-device-control.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
if (-not (Test-Path $ExePath)) { throw "Build produced no executable at $ExePath" }

$folderMb = [math]::Round((Get-ChildItem $DistDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Ok "Built $DistName ($folderMb MB across $((Get-ChildItem $DistDir -Recurse -File).Count) files)."

# ------------------------------------------------------------ smoke check --
Write-Step "Verifying the build does not unpack itself at startup"
Get-ChildItem $env:TEMP -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
$before = @(Get-ChildItem $env:TEMP -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue).Count
$started = Get-Date
$proc = Start-Process $ExePath -PassThru
$deadline = $started.AddSeconds(60)
while ((Get-Date) -lt $deadline -and -not $proc.MainWindowHandle) {
    Start-Sleep -Milliseconds 100
    $proc.Refresh()
}
$elapsed = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
Start-Sleep -Seconds 2
$after = @(Get-ChildItem $env:TEMP -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue).Count
if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }

if (-not $proc.MainWindowHandle -and $elapsed -ge 60) {
    throw "The application never showed a window within 60 seconds."
}
if (($after - $before) -ne 0) {
    throw "The build still extracts to %TEMP% ($($after - $before) _MEI directories). Check the spec."
}
Write-Ok "Window appeared in ${elapsed}s with no temporary extraction."

# ----------------------------------------------------------------- sign ----
if ($Sign) {
    Write-Step "Signing"
    if (-not $CertThumbprint) {
        throw "-Sign needs -CertThumbprint or the SIGN_THUMBPRINT environment variable."
    }
    $signtool = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin' -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match 'x64' } |
        Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $signtool) { throw "signtool.exe not found. Install the Windows SDK." }

    & $signtool.FullName sign /sha1 $CertThumbprint /fd SHA256 `
        /tr $TimestampUrl /td SHA256 /v $ExePath
    if ($LASTEXITCODE -ne 0) { throw "Signing the executable failed." }
    Write-Ok "Executable signed and timestamped."
} else {
    Write-Warn "Unsigned build. Defender detection is fixed by the packaging, but"
    Write-Warn "SmartScreen may still show 'unknown publisher' until it is signed."
}

# ------------------------------------------------------------ portable zip --
Write-Step "Creating the portable archive"
$zipPath = Join-Path $PSScriptRoot "dist\ConvrseDeviceControl-$AppVersion-portable.zip"
Remove-Item $zipPath -ErrorAction SilentlyContinue
Compress-Archive -Path $DistDir -DestinationPath $zipPath -CompressionLevel Optimal
Write-Ok "$([System.IO.Path]::GetFileName($zipPath)) ($([math]::Round((Get-Item $zipPath).Length/1MB,1)) MB)"

# -------------------------------------------------------------- installer --
Write-Step "Building the installer"
# Inno Setup installs to Program Files when run elevated and to the per-user
# Programs folder otherwise, which is where winget puts it. Check both.
$iscc = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe',
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    $onPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($onPath) { $iscc = $onPath.Source }
}

if (-not $iscc) {
    Write-Warn "Inno Setup 6 not found, so no installer was produced."
    Write-Warn "Install it from https://jrsoftware.org/isdl.php and re-run."
} else {
    New-Item -ItemType Directory -Force -Path "$PSScriptRoot\dist\installer" | Out-Null
    & $iscc "/DSourceDir=$DistDir" "$PSScriptRoot\installer\convrse-device-control.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
    $setup = Join-Path $PSScriptRoot "dist\installer\ConvrseDeviceControl-$AppVersion-Setup.exe"
    if ($Sign -and (Test-Path $setup)) {
        & $signtool.FullName sign /sha1 $CertThumbprint /fd SHA256 `
            /tr $TimestampUrl /td SHA256 /v $setup
        if ($LASTEXITCODE -ne 0) { throw "Signing the installer failed." }
    }
    Write-Ok "Installer written to dist\installer."
}

Write-Step "Done"
Write-Host "  Application : $DistDir" -ForegroundColor Green
Write-Host "  Portable    : $zipPath" -ForegroundColor Green
if ($iscc) { Write-Host "  Installer   : $PSScriptRoot\dist\installer" -ForegroundColor Green }
