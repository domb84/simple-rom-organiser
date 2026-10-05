# Fetch libsndfile (LGPL-2.1+, contains libFLAC) for the optional native FLAC decoder (romorg/nativeflac.py).
# The Windows DLL is taken from the official "soundfile" wheel on PyPI and cached in packaging\.cache\native.
# Prints the path of the DLL. Used by build_windows*.ps1 -BundleSndfile.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir = "$root\packaging\.cache\native"
$dll = "$dir\libsndfile-1.dll"
if (-not (Test-Path $dll)) {
    New-Item -ItemType Directory -Force $dir | Out-Null
    $tmp = Join-Path $env:TEMP "romorg-sndfile-wheel"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    python -m pip download soundfile --no-deps --only-binary=:all: --platform win_amd64 --python-version 3.13 -d $tmp | Out-Null
    if ($LASTEXITCODE) { throw "pip download soundfile failed" }
    $whl = Get-ChildItem "$tmp\soundfile-*.whl" | Select-Object -First 1
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($whl.FullName)
    try {
        foreach ($e in $z.Entries) {
            if ($e.FullName -like "_soundfile_data/libsndfile_x64.dll") {
                [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dll, $true)
            }
        }
    } finally { $z.Dispose() }
    if (-not (Test-Path $dll)) { throw "libsndfile_x64.dll not found in $($whl.Name)" }
}
Write-Output $dll
