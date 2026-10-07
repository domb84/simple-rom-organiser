# Build the Windows package: dist\Simple_ROM_Organiser-<version>-win64.zip
#
# Bundles the official CPython "embeddable" zip (no installer, no admin rights needed) with the romorg
# package and a launcher. Unzip anywhere and double-click Simple ROM Organiser.vbs.
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 [-PyVersion 3.14.8]
# Python 3.14 brings compression.zstd (fast Zstandard CHDs and the Zstandard preset) without a separate DLL.
param([string]$PyVersion = "3.14.8",
      [switch]$NoSndfile,   # leave out libsndfile (LGPL, native FLAC decoding through its virtual I/O)
      [switch]$NoFlac)      # leave out libFLAC (BSD-3-Clause, FLAC encoding for the CHD writer + decoding)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = (Select-String -Path "$root\romorg\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$cache = "$root\packaging\.cache"; New-Item -ItemType Directory -Force $cache | Out-Null
$zipName = "python-$PyVersion-embed-amd64.zip"
# SHA-256 of the python.org embeddable zip, per version (a version not listed here is used unchecked, with a warning)
$pins = @{ "3.14.8" = "a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310" }
$zipOk = {
    if (-not (Test-Path "$cache\$zipName")) { return $false }
    if (-not $pins.ContainsKey($PyVersion)) { return $true }
    return (Get-FileHash "$cache\$zipName" -Algorithm SHA256).Hash -eq $pins[$PyVersion]
}
if (-not (& $zipOk)) {
    Invoke-WebRequest "https://www.python.org/ftp/python/$PyVersion/$zipName" -OutFile "$cache\$zipName" -UseBasicParsing
    if (-not (& $zipOk)) { throw "$zipName does not match its pinned SHA-256" }
}
if (-not $pins.ContainsKey($PyVersion)) { Write-Warning "no pinned SHA-256 for $zipName" }

$stage = "$root\build\windows\Simple_ROM_Organiser"
if (Test-Path "$root\build\windows") { Remove-Item -Recurse -Force "$root\build\windows" }
New-Item -ItemType Directory -Force "$stage\python", "$stage\app" | Out-Null
Expand-Archive "$cache\$zipName" "$stage\python"

# Isolated embedded mode: the ._pth file replaces sys.path. "..\app" holds romorg.
$pth = Get-ChildItem "$stage\python\python*._pth" | Select-Object -First 1
$stdlib = (Get-Content $pth.FullName | Where-Object { $_ -like "python*.zip" } | Select-Object -First 1)
Set-Content $pth.FullName -Encoding ascii @($stdlib, ".", "..\app", "import site")

Copy-Item -Recurse "$root\romorg" "$stage\app\romorg"
New-Item -ItemType Directory -Force "$stage\licenses" | Out-Null
Copy-Item "$PSScriptRoot\THIRD_PARTY_NOTICES.txt" "$stage\THIRD_PARTY_NOTICES.txt"
Copy-Item "$stage\python\LICENSE.txt" "$stage\licenses\python-LICENSE.txt"
if (-not $NoSndfile) {
    # LGPL: shipped as a separate, replaceable DLL (loaded with ctypes), with its licence text and source pointers
    $dll = & "$PSScriptRoot\fetch_sndfile.ps1"
    New-Item -ItemType Directory -Force "$stage\app\native" | Out-Null
    Copy-Item $dll "$stage\app\native\libsndfile-1.dll"
    Copy-Item (Join-Path (Split-Path $dll) "COPYING") "$stage\licenses\libsndfile-COPYING.txt"
    Copy-Item (Join-Path (Split-Path $dll) "license_notes.md") "$stage\licenses\libsndfile-license_notes.md"
}
if (-not $NoFlac) {
    # BSD-3-Clause: the official Xiph.Org build (imports only KERNEL32 / msvcrt), loaded with ctypes by flacnative.py
    $flac = & "$PSScriptRoot\fetch_flac.ps1"
    New-Item -ItemType Directory -Force "$stage\app\native" | Out-Null
    Copy-Item $flac "$stage\app\native\libFLAC.dll"
    Copy-Item (Join-Path (Split-Path $flac) "COPYING.Xiph") "$stage\licenses\FLAC-COPYING.Xiph.txt"
    Copy-Item (Join-Path (Split-Path $flac) "FLAC-AUTHORS") "$stage\licenses\FLAC-AUTHORS.txt"
    Copy-Item (Join-Path (Split-Path $flac) "libogg-COPYING") "$stage\licenses\libogg-COPYING.txt"   # linked into libFLAC.dll
    # the MinGW-w64 runtime and winpthreads are linked into libFLAC.dll too (see fetch_flac.ps1)
    Copy-Item (Join-Path (Split-Path $flac) "winpthreads-COPYING") "$stage\licenses\winpthreads-COPYING.txt"
    Copy-Item (Join-Path (Split-Path $flac) "mingw-w64-runtime-COPYING") "$stage\licenses\mingw-w64-runtime-COPYING.txt"
}
Get-ChildItem "$stage\app" -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
& "$stage\python\python.exe" -m compileall -q -d "app\romorg" --invalidation-mode unchecked-hash "$stage\app\romorg"
if ($LASTEXITCODE) { throw "compileall failed" }

# Launcher: no console window; output goes to %LOCALAPPDATA%\simple-rom-organiser\app.log
@'
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
logDir = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\simple-rom-organiser"
If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir
cmd = "cmd /c """"" & here & "\python\python.exe"" -I -u -m romorg >> """ & logDir & "\app.log"" 2>&1"""
sh.CurrentDirectory = here
sh.Run cmd, 0, False
'@ | Set-Content "$stage\Simple ROM Organiser.vbs" -Encoding ascii
@'
@echo off
rem Console version (shows log output). Extra options: --port N --no-browser --no-update --verbose
rem "--self-check" tests the CHD engine of this package (libFLAC, Zstandard, worker processes, the writer).
"%~dp0python\python.exe" -I -u -m romorg %*
'@ | Set-Content "$stage\Simple ROM Organiser (console).cmd" -Encoding ascii
@"
Simple ROM Organiser $version (Windows, 64-bit)

Double-click "Simple ROM Organiser.vbs". It starts a local server and opens your browser.
Quit with the Quit button in the top bar (closing the tab does not stop it).
Data (DATs, settings) and app.log live in %LOCALAPPDATA%\simple-rom-organiser.
Use "Simple ROM Organiser (console).cmd" to see log output or pass options.
The app reads, checks and creates CHD files itself: chdman is not needed. An installed chdman is still
found (on PATH, or chdman from the MAME download at mamedev.org placed in this folder) and can be
chosen in the Convert step.
native\ (inside app\) holds libFLAC (BSD licence: FLAC audio when CHDs are read and written) and libsndfile
(a replaceable LGPL library); see THIRD_PARTY_NOTICES.txt and the licenses folder. 7-Zip is not included.
No installation or admin rights needed; delete the folder to remove the app.
"@ | Set-Content "$stage\README.txt" -Encoding ascii

# The checks below must see only what the package brings, never a developer's environment.
foreach ($v in "ROMORG_LIBFLAC", "ROMORG_SNDFILE", "ROMORG_LIBZSTD", "ROMORG_NO_NATIVE_FLAC", "ROMORG_NATIVE_FLAC") {
    Remove-Item "Env:$v" -ErrorAction SilentlyContinue
}

# Smoke test: bundled interpreter can serve the UI.
$data = "$root\build\windows\smoke-data"
$env:ROMORG_DATA_DIR = $data; $env:ROMORG_OFFLINE = "1"
$port = Get-Random -Minimum 20000 -Maximum 50000
$p = Start-Process "$stage\python\python.exe" "-I -u -m romorg --no-browser --port $port" -PassThru -WindowStyle Hidden
try {
    $ok = $false
    for ($i = 0; $i -lt 100 -and -not $ok; $i++) {
        Start-Sleep -Milliseconds 200
        try { $r = Invoke-WebRequest "http://127.0.0.1:$port/api/status" -UseBasicParsing; $ok = $r.Content -match '"version"' } catch {}
    }
    if (-not $ok) { throw "smoke test failed: /api/status did not answer" }
    Write-Host "smoke test ok: $($r.Content.Substring(0, [Math]::Min(120, $r.Content.Length)))"
} finally {
    if (-not $p.HasExited) { Stop-Process $p.Id -Force }
    $p.WaitForExit()
    Remove-Item Env:ROMORG_DATA_DIR, Env:ROMORG_OFFLINE
}
Remove-Item -Recurse -Force $data -ErrorAction SilentlyContinue

$env:PYTHONDONTWRITEBYTECODE = "1"
if (-not $NoSndfile) {   # the embedded Python must find and load the bundled libsndfile
    $native = & "$stage\python\python.exe" -I -c "from romorg import nativeflac; print(nativeflac.available())"
    if ($native -ne "True") { throw "self-test failed: the bundled libsndfile could not be loaded" }
    Write-Host "native FLAC decoder loads from native\libsndfile-1.dll"
}
# CHD engine self-check inside the package: Zstandard (compression.zstd), libFLAC from app\native, the scheduler
# with worker processes, and the writer making a CHD with FLAC audio in worker processes.
$checkArgs = @("-I", "-m", "romorg", "--self-check")
if (-not $NoFlac) { $checkArgs += "--require-native" }
$out = & "$stage\python\python.exe" @checkArgs
$rc = $LASTEXITCODE
Remove-Item Env:PYTHONDONTWRITEBYTECODE
$out | ForEach-Object { Write-Host $_ }
if ($rc) { throw "CHD engine self-check failed" }
if (-not $NoFlac) {
    $want = [regex]::Escape("OK    libFLAC $stage\app\native\libFLAC.dll")
    if (-not ($out | Where-Object { $_ -match "^$want" })) { throw "libFLAC was not loaded from app\native" }
}

New-Item -ItemType Directory -Force "$root\dist" | Out-Null
$out = "$root\dist\Simple_ROM_Organiser-$version-win64.zip"
if (Test-Path $out) { Remove-Item $out }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $out, [IO.Compression.CompressionLevel]::Optimal, $true)
Write-Host ("Built {0} ({1:N1} MB)" -f $out, ((Get-Item $out).Length / 1MB))
