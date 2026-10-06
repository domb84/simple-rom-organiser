# Build a single-file Windows executable with PyInstaller: dist\Simple_ROM_Organiser-<version>-win64.exe
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows_exe.ps1
# Needs Python 3.14 (the py launcher's "py -3.14", or -Python <path to python.exe>). PyInstaller (the pinned
# -PyInstallerVersion) is installed into a build venv (packaging\.cache\venv-pyinstaller-<series>), never into that
# Python. 3.14 brings compression.zstd.
param([switch]$BundleSndfile,   # include libsndfile (LGPL) for native FLAC decoding through its virtual I/O
      [switch]$NoFlac,          # leave out libFLAC (BSD-3-Clause: FLAC encoding for the CHD writer + decoding)
      [string]$Python = "",     # the interpreter to build with (default: py -3.14)
      [string]$PySeries = "3.14",
      [string]$PyInstallerVersion = "6.22.3")   # pinned: a new PyInstaller release changes what goes into the exe
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = (Select-String -Path "$root\romorg\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Set-Location $root

if (-not $Python) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "no py launcher: pass -Python <path to a Python $PySeries python.exe>" }
    $Python = (& py "-$PySeries" -c "import sys; print(sys.executable)")
    if ($LASTEXITCODE -or -not $Python) { throw "Python $PySeries is not installed (py -$PySeries failed)" }
}
$series = (& $Python -c "import sys; print('%d.%d' % sys.version_info[:2])")
if ($series -ne $PySeries) { throw "$Python is Python $series, the build wants $PySeries" }
$venv = "$root\packaging\.cache\venv-pyinstaller-$PySeries"
$py = "$venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    & $Python -m venv $venv
    if ($LASTEXITCODE) { throw "could not create the build venv $venv" }
}
$ErrorActionPreference = "Continue"     # Windows PowerShell turns a native command's stderr into an error under Stop
$have = & $py -c "import PyInstaller; print(PyInstaller.__version__)" 2>&1
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -or "$have" -ne $PyInstallerVersion) {
    & $py -m pip install --disable-pip-version-check "pyinstaller==$PyInstallerVersion"
    if ($LASTEXITCODE) { throw "pip install pyinstaller==$PyInstallerVersion failed" }
}
Write-Host "PyInstaller $(& $py -m PyInstaller --version) on Python $(& $py -c 'import sys; print(sys.version.split()[0])')"

$work = "$root\build\pyinstaller"
New-Item -ItemType Directory -Force $work | Out-Null
& $py packaging\make_icon.py $work
$ico = "$work\simple-rom-organiser.ico"
# Wrap the PNG in a single-image .ico (PNG-in-ICO is supported since Vista).
$png = [IO.File]::ReadAllBytes("$work\simple-rom-organiser.png")
$ms = New-Object IO.MemoryStream; $bw = New-Object IO.BinaryWriter $ms
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]1)
$bw.Write([byte]0); $bw.Write([byte]0); $bw.Write([byte]0); $bw.Write([byte]0)
$bw.Write([uint16]1); $bw.Write([uint16]32); $bw.Write([uint32]$png.Length); $bw.Write([uint32]22)
$bw.Write($png); $bw.Flush(); [IO.File]::WriteAllBytes($ico, $ms.ToArray())

# Licence texts travel inside the exe (licenses\ in the bundle) and next to it in dist\.
$lic = "$work\licenses"
if (Test-Path $lic) { Remove-Item -Recurse -Force $lic }
New-Item -ItemType Directory -Force $lic | Out-Null
$extra = @()
if (-not $NoFlac) {
    $flac = & "$PSScriptRoot\fetch_flac.ps1"
    $extra += @("--add-binary", "$flac;native")             # found by flacnative._candidates() in _MEIPASS\native
    Copy-Item (Join-Path (Split-Path $flac) "COPYING.Xiph") "$lic\FLAC-COPYING.Xiph.txt"
    Copy-Item (Join-Path (Split-Path $flac) "FLAC-AUTHORS") "$lic\FLAC-AUTHORS.txt"
    Copy-Item (Join-Path (Split-Path $flac) "libogg-COPYING") "$lic\libogg-COPYING.txt"     # linked into libFLAC.dll
    # the MinGW-w64 runtime and winpthreads are linked into libFLAC.dll too (see fetch_flac.ps1)
    Copy-Item (Join-Path (Split-Path $flac) "winpthreads-COPYING") "$lic\winpthreads-COPYING.txt"
    Copy-Item (Join-Path (Split-Path $flac) "mingw-w64-runtime-COPYING") "$lic\mingw-w64-runtime-COPYING.txt"
}
if ($BundleSndfile) {
    $snd = & "$PSScriptRoot\fetch_sndfile.ps1"
    $extra += @("--add-binary", "$snd;.")
    Copy-Item (Join-Path (Split-Path $snd) "COPYING") "$lic\libsndfile-COPYING.txt"
    Copy-Item (Join-Path (Split-Path $snd) "license_notes.md") "$lic\libsndfile-license_notes.md"
}
$pyLicense = Join-Path (& $Python -c "import sys; print(sys.base_prefix)") "LICENSE.txt"
if (-not (Test-Path $pyLicense)) { throw "Python's licence text $pyLicense is missing: the exe must ship it" }
Copy-Item $pyLicense "$lic\python-LICENSE.txt"
Copy-Item "$PSScriptRoot\THIRD_PARTY_NOTICES.txt" "$lic\THIRD_PARTY_NOTICES.txt"
$extra += @("--add-data", "$lic;licenses")
& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "Simple_ROM_Organiser-$version-win64" --icon $ico `
    --add-data "$root\romorg\static;romorg\static" `
    --collect-submodules romorg `
    @extra `
    --distpath "$root\dist" --workpath "$work\work" --specpath "$work" `
    --paths $root `
    "$root\packaging\windows_entry.py"
if ($LASTEXITCODE) { throw "PyInstaller failed" }

# The checks below must see only what the exe brings, never a developer's environment.
foreach ($v in "ROMORG_LIBFLAC", "ROMORG_SNDFILE", "ROMORG_LIBZSTD", "ROMORG_NO_NATIVE_FLAC", "ROMORG_NATIVE_FLAC") {
    Remove-Item "Env:$v" -ErrorAction SilentlyContinue
}
# Smoke test the real exe.
$exe = "$root\dist\Simple_ROM_Organiser-$version-win64.exe"
# Paths over 260 characters need the exe's manifest to say longPathAware (PyInstaller's default manifest does; the
# build must notice if a new release stops doing so). Windows then honours the LongPathsEnabled setting for the exe.
$manifest = [Text.Encoding]::GetEncoding(28591).GetString([IO.File]::ReadAllBytes($exe))
if ($manifest -notmatch '<longPathAware[^>]*>\s*true\s*</longPathAware>') { throw "the exe's manifest lacks longPathAware" }
$mods = (Get-ChildItem "$root\romorg\*.py" | Where-Object { $_.BaseName -ne "__init__" } | ForEach-Object { $_.BaseName }) -join ","
$st = Start-Process $exe "--selftest $mods" -Wait -PassThru
if ($st.ExitCode -ne 0) { throw "self-test failed: $($st.ExitCode) romorg module(s) are missing from the exe" }
# CHD engine self-check inside the exe (windowed: the report goes to a file): Zstandard (compression.zstd), libFLAC
# from the bundle, the scheduler with worker processes (the exe started as "--chd-worker"), the writer with FLAC audio.
$report = "$work\selfcheck.txt"
Remove-Item $report -ErrorAction SilentlyContinue
$checkArgs = "--self-check --report `"$report`"" + $(if ($NoFlac) { "" } else { " --require-native" })
$st = Start-Process $exe $checkArgs -Wait -PassThru
$lines = if (Test-Path $report) { Get-Content $report } else { @("(no report written)") }
$lines | ForEach-Object { Write-Host $_ }
if ($st.ExitCode -ne 0) { throw "CHD engine self-check of the exe failed ($($st.ExitCode))" }
if (-not $NoFlac -and -not ($lines | Where-Object { $_ -match '^OK    libFLAC .*\\native\\libFLAC\.dll' })) {
    throw "libFLAC was not loaded from the exe's bundle"
}
$data = "$work\smoke-data"
$env:ROMORG_DATA_DIR = $data; $env:ROMORG_OFFLINE = "1"
$port = Get-Random -Minimum 20000 -Maximum 50000
$p = Start-Process $exe "--no-browser --port $port" -PassThru
try {
    $ok = $false
    for ($i = 0; $i -lt 150 -and -not $ok; $i++) {
        Start-Sleep -Milliseconds 200
        try { $r = Invoke-WebRequest "http://127.0.0.1:$port/" -UseBasicParsing; $ok = $r.Content -match "<html" } catch {}
    }
    if (-not $ok) { throw "smoke test failed: UI did not answer" }
    $s = Invoke-WebRequest "http://127.0.0.1:$port/api/status" -UseBasicParsing
    Write-Host "smoke test ok: $($s.Content.Substring(0, [Math]::Min(100, $s.Content.Length)))"
} finally {
    Get-Process | Where-Object { $_.Path -eq $exe } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    Remove-Item Env:ROMORG_DATA_DIR, Env:ROMORG_OFFLINE
    Remove-Item -Recurse -Force $data -ErrorAction SilentlyContinue
}
# The notices and licence texts also go next to the exe: ship dist\THIRD_PARTY_NOTICES.txt and dist\licenses with it.
$distLic = "$root\dist\licenses"
New-Item -ItemType Directory -Force $distLic | Out-Null
Get-ChildItem $lic -File | Where-Object { $_.Name -ne "THIRD_PARTY_NOTICES.txt" } | Copy-Item -Destination $distLic
Copy-Item "$PSScriptRoot\THIRD_PARTY_NOTICES.txt" "$root\dist\THIRD_PARTY_NOTICES.txt"
Write-Host "licence texts: dist\THIRD_PARTY_NOTICES.txt and dist\licenses (also inside the exe)"
Write-Host ("Built {0} ({1:N1} MB)" -f $exe, ((Get-Item $exe).Length / 1MB))
