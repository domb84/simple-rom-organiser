# Build the Windows package: dist\Simple_ROM_Organiser-<version>-win64.zip
#
# Bundles the official CPython "embeddable" zip (no installer, no admin rights needed) with the romorg
# package and a launcher. Unzip anywhere and double-click Simple ROM Organiser.vbs.
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 [-PyVersion 3.13.15]
param([string]$PyVersion = "3.13.15")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = (Select-String -Path "$root\romorg\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$cache = "$root\packaging\.cache"; New-Item -ItemType Directory -Force $cache | Out-Null
$zipName = "python-$PyVersion-embed-amd64.zip"
if (-not (Test-Path "$cache\$zipName")) {
    Invoke-WebRequest "https://www.python.org/ftp/python/$PyVersion/$zipName" -OutFile "$cache\$zipName"
}

$stage = "$root\build\windows\Simple_ROM_Organiser"
if (Test-Path "$root\build\windows") { Remove-Item -Recurse -Force "$root\build\windows" }
New-Item -ItemType Directory -Force "$stage\python", "$stage\app" | Out-Null
Expand-Archive "$cache\$zipName" "$stage\python"

# Isolated embedded mode: the ._pth file replaces sys.path. "..\app" holds romorg.
$pth = Get-ChildItem "$stage\python\python*._pth" | Select-Object -First 1
$stdlib = (Get-Content $pth.FullName | Where-Object { $_ -like "python*.zip" } | Select-Object -First 1)
Set-Content $pth.FullName -Encoding ascii @($stdlib, ".", "..\app", "import site")

Copy-Item -Recurse "$root\romorg" "$stage\app\romorg"
Get-ChildItem "$stage\app" -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
& "$stage\python\python.exe" -m compileall -q --invalidation-mode unchecked-hash "$stage\app\romorg"
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
"%~dp0python\python.exe" -I -u -m romorg %*
'@ | Set-Content "$stage\Simple ROM Organiser (console).cmd" -Encoding ascii
@"
Simple ROM Organiser $version (Windows, 64-bit)

Double-click "Simple ROM Organiser.vbs". It starts a local server and opens your browser.
Quit with the Quit button in the top bar (closing the tab does not stop it).
Data (DATs, settings) and app.log live in %LOCALAPPDATA%\simple-rom-organiser.
Use "Simple ROM Organiser (console).cmd" to see log output or pass options.
No installation or admin rights needed; delete the folder to remove the app.
"@ | Set-Content "$stage\README.txt" -Encoding ascii

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

New-Item -ItemType Directory -Force "$root\dist" | Out-Null
$out = "$root\dist\Simple_ROM_Organiser-$version-win64.zip"
if (Test-Path $out) { Remove-Item $out }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $out, [IO.Compression.CompressionLevel]::Optimal, $true)
Write-Host ("Built {0} ({1:N1} MB)" -f $out, ((Get-Item $out).Length / 1MB))
