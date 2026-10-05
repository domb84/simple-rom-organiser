# Fetch libsndfile (LGPL-2.1+, contains libFLAC) for the optional native FLAC decoder (romorg/nativeflac.py).
# The Windows DLL and its licence files are taken from the official "soundfile" wheel on PyPI and cached in
# packaging\.cache\native (libsndfile-1.dll, COPYING = the LGPL text, license_notes.md = copyrights + source links).
# Prints the path of the DLL. Used by build_windows*.ps1.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir = "$root\packaging\.cache\native"
$dll = "$dir\libsndfile-1.dll"
if (-not ((Test-Path $dll) -and (Test-Path "$dir\COPYING") -and (Test-Path "$dir\license_notes.md"))) {
    New-Item -ItemType Directory -Force $dir | Out-Null
    $tmp = Join-Path $env:TEMP "romorg-sndfile-wheel"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    python -m pip download soundfile --no-deps --only-binary=:all: --platform win_amd64 --python-version 3.13 -d $tmp | Out-Null
    if ($LASTEXITCODE) { throw "pip download soundfile failed" }
    $whl = Get-ChildItem "$tmp\soundfile-*.whl" | Select-Object -First 1
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
