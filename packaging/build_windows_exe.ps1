# Build a single-file Windows executable with PyInstaller: dist\Simple_ROM_Organiser-<version>-win64.exe
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows_exe.ps1
# Needs Python 3.11+ on PATH; installs PyInstaller (pip) if missing.
param([switch]$BundleSndfile)   # include libsndfile (LGPL) for native FLAC decoding: ~40x faster CD audio
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = (Select-String -Path "$root\romorg\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Set-Location $root
python -m PyInstaller --version *> $null
if ($LASTEXITCODE) { python -m pip install pyinstaller; if ($LASTEXITCODE) { throw "pip install failed" } }

$work = "$root\build\pyinstaller"
New-Item -ItemType Directory -Force $work | Out-Null
python packaging\make_icon.py $work
$ico = "$work\simple-rom-organiser.ico"
# Wrap the PNG in a single-image .ico (PNG-in-ICO is supported since Vista).
$png = [IO.File]::ReadAllBytes("$work\simple-rom-organiser.png")
$ms = New-Object IO.MemoryStream; $bw = New-Object IO.BinaryWriter $ms
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]1)
$bw.Write([byte]0); $bw.Write([byte]0); $bw.Write([byte]0); $bw.Write([byte]0)
$bw.Write([uint16]1); $bw.Write([uint16]32); $bw.Write([uint32]$png.Length); $bw.Write([uint32]22)
$bw.Write($png); $bw.Flush(); [IO.File]::WriteAllBytes($ico, $ms.ToArray())

$extra = @()
if ($BundleSndfile) { $extra += @("--add-binary", "$(& "$PSScriptRoot\fetch_sndfile.ps1");.") }
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "Simple_ROM_Organiser-$version-win64" --icon $ico `
    --add-data "$root\romorg\static;romorg\static" `
    --collect-submodules romorg `
    @extra `
    --distpath "$root\dist" --workpath "$work\work" --specpath "$work" `
    --paths $root `
    "$root\packaging\windows_entry.py"
if ($LASTEXITCODE) { throw "PyInstaller failed" }

# Smoke test the real exe.
$exe = "$root\dist\Simple_ROM_Organiser-$version-win64.exe"
$mods = (Get-ChildItem "$root\romorg\*.py" | Where-Object { $_.BaseName -ne "__init__" } | ForEach-Object { $_.BaseName }) -join ","
$st = Start-Process $exe "--selftest $mods" -Wait -PassThru
if ($st.ExitCode -ne 0) { throw "self-test failed: $($st.ExitCode) romorg module(s) are missing from the exe" }
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
Write-Host ("Built {0} ({1:N1} MB)" -f $exe, ((Get-Item $exe).Length / 1MB))
