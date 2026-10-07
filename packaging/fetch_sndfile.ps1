# Fetch libsndfile (LGPL-2.1+, contains libFLAC) for the optional native FLAC decoder (romorg/nativeflac.py).
# The Windows DLL and its licence files are taken from the official "soundfile" wheel on PyPI and cached in
# packaging\.cache\native (libsndfile-1.dll, COPYING = the LGPL text, license_notes.md = copyrights + source links).
# Prints the path of the DLL. Used by build_windows*.ps1.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir = "$root\packaging\.cache\native"
$dll = "$dir\libsndfile-1.dll"
# pinned: soundfile wheel version and its SHA-256 (bundles libsndfile; the DLL inside is checked too)
$SoundfileVersion = "0.14.0"
$WheelSha256 = "299491D3499460FB1B74BB4BD78B57FFC2D243A5FAFA7B6EC1B264875C78453E"   # soundfile-0.14.0-py2.py3-none-win_amd64.whl
$DllSha256 = "22518C16F9D13EDA5AE5ADF999C4740D7D16CC2D6B178B36DD4AC2F759A38559"     # _soundfile_data/libsndfile_x64.dll
if (-not ((Test-Path $dll) -and ((Get-FileHash $dll -Algorithm SHA256).Hash -eq $DllSha256) -and (Test-Path "$dir\COPYING") -and (Test-Path "$dir\license_notes.md"))) {
    New-Item -ItemType Directory -Force $dir | Out-Null
    $tmp = Join-Path $env:TEMP "romorg-sndfile-wheel"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    # pip of the build's Python series (the py launcher's 3.14 when present; "python" on PATH can be anything)
    $pyExe, $pyArgs = "python", @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $pyExe, $pyArgs = "py", @("-3.14") }
    & $pyExe @pyArgs -m pip download "soundfile==$SoundfileVersion" --no-deps --only-binary=:all: --platform win_amd64 --python-version 3.14 -d $tmp | Out-Null
    if ($LASTEXITCODE) { throw "pip download soundfile failed" }
    $whl = Get-ChildItem "$tmp\soundfile-*.whl" | Select-Object -First 1
    if ((Get-FileHash $whl.FullName -Algorithm SHA256).Hash -ne $WheelSha256) { throw "$($whl.Name) does not match its pinned SHA-256" }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $want = @{
        "_soundfile_data/libsndfile_x64.dll" = $dll
        "_soundfile_data/COPYING"            = "$dir\COPYING"
        "licensing/license_notes.md"         = "$dir\license_notes.md"
    }
    $z = [IO.Compression.ZipFile]::OpenRead($whl.FullName)
    try {
        foreach ($e in $z.Entries) {
            if ($want.ContainsKey($e.FullName)) {
                [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $want[$e.FullName], $true)
            }
        }
    } finally { $z.Dispose() }
    foreach ($f in $want.Values) { if (-not (Test-Path $f)) { throw "$f was not found in $($whl.Name)" } }
}
Write-Output $dll
