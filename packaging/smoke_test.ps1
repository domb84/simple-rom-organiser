# Smoke-test a built Windows package: the Windows counterpart of packaging/smoke_test.sh, with the same checks.
# Run by packaging\build_windows.ps1 (the .zip's own python.exe) and packaging\build_windows_exe.ps1 (the exe); a
# miss throws, which fails the build.
#   packaging\smoke_test.ps1 -Exe <program> [-ExeArgs "<arguments before --no-browser>"] -DataDir <throw-away folder>
#                            [-SelfCheck <the lines "--self-check --require-native" printed>] [-NoFlac]
# It starts the program with "--no-browser --port N" on a throw-away data folder (ROMORG_OFFLINE=1: no DAT download),
# asks for the page and the API, then quits it with POST /api/quit. Whatever happens, the WHOLE process tree is gone
# afterwards: the onefile exe runs the app in a child process, and a child left behind keeps dist\*.exe locked.
param([Parameter(Mandatory = $true)][string]$Exe,
      [string]$ExeArgs = "",
      [Parameter(Mandatory = $true)][string]$DataDir,
      [string[]]$SelfCheck = $null,
      [switch]$NoFlac)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"      # Invoke-WebRequest draws a progress bar for every request otherwise

function Assert-Match([string]$Text, [string]$Pattern, [string]$Message) {
    if ($Text -notmatch $Pattern) { throw "smoke test failed: $Message" }
}

# --- the CHD engine self-check of the package (the caller ran it and hands over the lines)
if ($null -ne $SelfCheck) {
    $report = ($SelfCheck -join "`n")
    Assert-Match $report '(?m)^SELF-CHECK PASSED' "the self-check did not pass"
    if (-not $NoFlac) {
        Assert-Match $report '(?m)^OK    package libFLAC .*[\\/]native[\\/]libFLAC\.dll' "libFLAC was not loaded from the package"
    }
    Assert-Match $report '(?m)^OK    package Zstandard: compression\.zstd' "Zstandard does not come from compression.zstd"
    Assert-Match $report '(?m)^OK    the scheduler' "scheduler check missing"
    Assert-Match $report '(?m)^OK    the writer ' "CHD writer check missing"
    Assert-Match $report '(?m)^OK    the app is complete' "the package lacks a module or a file of the web UI"
    if ($report -match '(?m)^FAIL ') { throw "smoke test failed: the self-check has a FAIL line" }
}

function Get-Tree([int]$RootPid) {
    # the process and everything it started (the onefile exe's child, CHD worker processes), deepest first
    $all = Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId
    $out = New-Object System.Collections.Generic.List[int]
    $todo = New-Object System.Collections.Generic.Queue[int]
    $todo.Enqueue($RootPid)
    while ($todo.Count) {
        $id = $todo.Dequeue()
        $out.Add($id)
        foreach ($c in $all) { if ($c.ParentProcessId -eq $id -and -not $out.Contains([int]$c.ProcessId)) { $todo.Enqueue([int]$c.ProcessId) } }
    }
    $out.Reverse()
    return $out
}

function Stop-Tree([int]$RootPid, [string]$Image) {
    foreach ($id in (Get-Tree $RootPid)) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    # a child whose parent is already gone is in no tree: anything still running from this very file goes too
    Get-Process | Where-Object { $_.Path -eq $Image } | Stop-Process -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 50; $i++) {
        if (-not (Get-Process | Where-Object { $_.Path -eq $Image })) { break }
        Start-Sleep -Milliseconds 100
    }
}

function Get-Text([string]$Path, [hashtable]$Headers = @{}) {
    $r = Invoke-WebRequest "$script:url$Path" -UseBasicParsing -Headers $Headers -TimeoutSec 60
    if ($r.StatusCode -ne 210) { throw "smoke test failed: GET $Path answered $($r.StatusCode)" }
    if ($r.Content -is [byte[]]) { return [Text.Encoding]::UTF8.GetString($r.Content) }
    return [string]$r.Content
}

$old = @{ ROMORG_DATA_DIR = $env:ROMORG_DATA_DIR; ROMORG_OFFLINE = $env:ROMORG_OFFLINE }
if (Test-Path $DataDir) { Remove-Item -Recurse -Force $DataDir }
$env:ROMORG_DATA_DIR = $DataDir; $env:ROMORG_OFFLINE = "1"
$port = Get-Random -Minimum 20000 -Maximum 50000
$script:url = "http://127.0.0.1:$port"
$arguments = ("$ExeArgs --no-browser --port $port").Trim()
$p = Start-Process $Exe $arguments -PassThru -WindowStyle Hidden
$image = (Resolve-Path $Exe).Path
$passed = $false
try {
    $up = $false
    for ($i = 0; $i -lt 150 -and -not $up; $i++) {
        Start-Sleep -Milliseconds 200
        if ($p.HasExited) { throw "smoke test failed: the app exited early (exit code $($p.ExitCode))" }
        try { $null = Invoke-WebRequest "$script:url/" -UseBasicParsing -TimeoutSec 5; $up = $true } catch { }
    }
    if (-not $up) { throw "smoke test failed: the UI did not answer on port $port" }

    $index = Get-Text "/"
    $status = Get-Text "/api/status"
    Write-Host "GET /           -> $($index.Length) characters"
    Write-Host "GET /api/status -> $($status.Substring(0, [Math]::Min(160, $status.Length)))..."
    Assert-Match $status '"version"' "status missing version"
    Assert-Match $status '"nointro"' "status missing the No-Intro block"
    Assert-Match $status '"updates"' "status missing the updates block"
    Assert-Match $status '"redump"' "status missing the Redump block"
    Assert-Match $status '"os": "windows"' "status does not say the app runs on Windows"

    # the pages of the UI: home, a system, and the three v0.2 added; and the files the page loads
    foreach ($view in "view-home", "view-system", "view-retroarch", "view-chd", "view-settings", "view-databases", "view-bios") {
        Assert-Match $index ('id="' + $view + '"') "the page lacks $view"
    }
    foreach ($file in [regex]::Matches($index, '(?:src|href)="(/static/[^"]+)"') | ForEach-Object { $_.Groups[1].Value }) {
        if ((Get-Text $file).Length -lt 1000) { throw "smoke test failed: $file is empty" }
        Write-Host "GET $file -> ok"
    }

    if ($index -notmatch 'name="romorg-token" content="([^"]+)"') { throw "smoke test failed: the page has no token" }
    $token = $Matches[1]
    $auth = @{ "X-Romorg-Token" = $token }
    $platforms = Get-Text "/api/platforms" $auth
    foreach ($name in "Commodore Amiga", "Commodore Amiga - WHDLoad", "Nintendo Game Boy Advance", "Nintendo 64",
                      "Nintendo Entertainment System", "Super Nintendo Entertainment System", "Nintendo Game Boy",
                      "Nintendo Game Boy Color", "Nintendo DS", "Sega Mega Drive - Genesis", "Sega Master System",
                      "Sega Game Gear", "Sega 32X", "Atari Lynx", "Sega Dreamcast", "Sony PlayStation",
                      "Sony PlayStation 2", "Nintendo GameCube", "Nintendo Wii", "Nintendo Wii U", "Nintendo Switch") {
        Assert-Match $platforms ('"' + [regex]::Escape($name) + '"') "platform missing: $name"
    }
    $count = [regex]::Matches($platforms, '"folder_hint"').Count
    if ($count -ne 21) { throw "smoke test failed: expected 21 systems, the app lists $count" }
    Write-Host "GET /api/platforms -> $count systems"
    $updates = Get-Text "/api/updates"
    Assert-Match $updates '"state"' "/api/updates missing"
    Assert-Match $updates '"redump"' "/api/updates missing the Redump source"
    Assert-Match (Get-Text "/api/library/profile?platform=Commodore%20Amiga") '"rules"' "/api/library/profile missing the rules"
    Write-Host "GET /api/updates, /api/library/profile -> ok"
    $chdman = Get-Text "/api/chdman"
    Assert-Match $chdman '"found"' "/api/chdman missing"
    Assert-Match (Get-Text "/api/library/profile?platform=Sega%20Dreamcast") '"style": "redump"' "Dreamcast rules missing"
    Write-Host "GET /api/chdman, Dreamcast rules -> ok"
    if (-not $NoFlac) { Assert-Match $chdman '"native": true' "/api/chdman does not report native FLAC: $chdman" }
    Assert-Match $chdman '"writer": "auto"' "/api/chdman does not report the built-in writer: $chdman"
    Write-Host "GET /api/chdman -> built-in writer, native FLAC"
    # the pages v0.2 added answer with JSON (their modules are imported by name, on first use)
    $defaultsText = Get-Text "/api/library/defaults"
    Assert-Match $defaultsText '"catalog"' "/api/library/defaults missing"
    $defaults = $defaultsText | ConvertFrom-Json
    if ($null -eq $defaults.profile -or $null -eq $defaults.catalog) { throw "smoke test failed: /api/library/defaults lacks the rules" }
    $retroarchText = Get-Text "/api/retroarch"
    Assert-Match $retroarchText '"installs"' "/api/retroarch missing"
    $retroarch = $retroarchText | ConvertFrom-Json
    if ($null -eq $retroarch.PSObject.Properties["installs"]) { throw "smoke test failed: /api/retroarch lacks the installs" }
    Write-Host "GET /api/library/defaults, /api/retroarch -> ok"

    $null = Invoke-WebRequest "$script:url/api/quit" -Method Post -UseBasicParsing -TimeoutSec 30 `
        -Headers $auth -ContentType "application/json" -Body "{}"
    for ($i = 0; $i -lt 150 -and -not $p.HasExited; $i++) { Start-Sleep -Milliseconds 100 }
    if (-not $p.HasExited) { throw "smoke test failed: POST /api/quit did not stop the app" }
    for ($i = 0; $i -lt 50; $i++) {      # the onefile exe's child, CHD workers: nothing of this program may be left
        if (-not (Get-Process | Where-Object { $_.Path -eq $image })) { break }
        Start-Sleep -Milliseconds 100
    }
    if (Get-Process | Where-Object { $_.Path -eq $image }) { throw "smoke test failed: a process of the app is still running after Quit" }
    Write-Host "POST /api/quit -> process exited"
    $passed = $true
} finally {
    if (-not $passed) {
        $log = Join-Path $DataDir "app.log"
        if (Test-Path $log) { Write-Host "--- app.log"; Get-Content $log -Tail 40 | ForEach-Object { Write-Host $_ } }
    }
    Stop-Tree $p.Id $image
    foreach ($k in $old.Keys) {
        if ($null -eq $old[$k]) { Remove-Item "Env:$k" -ErrorAction SilentlyContinue } else { Set-Item "Env:$k" $old[$k] }
    }
    Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue
}
Write-Host "SMOKE TEST PASSED"
