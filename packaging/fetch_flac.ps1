# Fetch libFLAC (BSD-3-Clause) for the native FLAC encoder and decoder (romorg/flacenc.py, romorg/flacnative.py).
# The DLL comes from the official Xiph.Org Windows release of FLAC (flac-<version>-win.zip, a MinGW build whose only
# imports are KERNEL32.dll and msvcrt.dll, both part of Windows: no other DLL has to ship with it). That build has
# Ogg FLAC support compiled in (libogg, BSD-3-Clause, linked statically), so libogg's licence text ships too; the
# release zip does not carry it, so COPYING is taken from the official libogg source release.
# Both archives are checked against pinned SHA-256s and the files cached in packaging\.cache\native:
#   libFLAC.dll, COPYING.Xiph (FLAC's BSD licence), FLAC-AUTHORS, libogg-COPYING
# Prints the path of the DLL. Used by build_windows*.ps1.
$ErrorActionPreference = "Stop"
$FlacVersion = "1.5.0"
$FlacZipSha256 = "53f1500f0d6e7c61379d7fee50d4a9f7f504c650009506d9ba015530d76c0dde"    # flac-1.5.0-win.zip
$DllSha256 = "f93499172875fc2c0df80b57086f32e3f39e835283952ee2a59a3d4ffb097644"        # flac-1.5.0-win/Win64/libFLAC.dll
$OggVersion = "1.3.5"                                                                  # current when FLAC 1.5.0 was built
$OggZipSha256 = "fd4e5ba7e93b84b3ec41cdf01494cc586ef6e912b313dbab25512dd02665dfaf"     # libogg-1.3.5.zip
$root = Split-Path -Parent $PSScriptRoot
$cache = "$root\packaging\.cache"
$dir = "$cache\native"
$dll = "$dir\libFLAC.dll"

function Get-Pinned([string]$name, [string]$sha, [string[]]$urls) {
    $zip = "$cache\$name"
    if ((Test-Path $zip) -and ((Get-FileHash $zip -Algorithm SHA256).Hash -eq $sha)) { return $zip }
    foreach ($url in $urls) {
        try {
            Invoke-WebRequest $url -OutFile $zip -UseBasicParsing
            if ((Get-FileHash $zip -Algorithm SHA256).Hash -eq $sha) { return $zip }
            Write-Warning "SHA-256 mismatch for $url"
        } catch { Write-Warning "download failed: $url ($_)" }
    }
    throw "could not fetch $name with SHA-256 $sha"
}

function Expand-Some([string]$zip, [hashtable]$want) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($e in $z.Entries) {
            if ($want.ContainsKey($e.FullName)) {
                [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $want[$e.FullName], $true)
            }
        }
    } finally { $z.Dispose() }
    foreach ($f in $want.Values) { if (-not (Test-Path $f)) { throw "$f was not found in $zip" } }
}

$ok = (Test-Path $dll) -and (Test-Path "$dir\COPYING.Xiph") -and (Test-Path "$dir\FLAC-AUTHORS") -and
      (Test-Path "$dir\libogg-COPYING") -and ((Get-FileHash $dll -Algorithm SHA256).Hash -eq $DllSha256)
if (-not $ok) {
    New-Item -ItemType Directory -Force $dir | Out-Null
    $flacZip = Get-Pinned "flac-$FlacVersion-win.zip" $FlacZipSha256 @(
        "https://ftp.osuosl.org/pub/xiph/releases/flac/flac-$FlacVersion-win.zip",       # where downloads.xiph.org redirects
        "https://github.com/xiph/flac/releases/download/$FlacVersion/flac-$FlacVersion-win.zip")
    Expand-Some $flacZip @{
        "flac-$FlacVersion-win/Win64/libFLAC.dll" = $dll
        "flac-$FlacVersion-win/COPYING.Xiph"      = "$dir\COPYING.Xiph"
        "flac-$FlacVersion-win/AUTHORS"           = "$dir\FLAC-AUTHORS"
    }
    if ((Get-FileHash $dll -Algorithm SHA256).Hash -ne $DllSha256) { throw "SHA-256 mismatch for libFLAC.dll" }
    $oggZip = Get-Pinned "libogg-$OggVersion.zip" $OggZipSha256 @(
        "https://ftp.osuosl.org/pub/xiph/releases/ogg/libogg-$OggVersion.zip")
    Expand-Some $oggZip @{ "libogg-$OggVersion/COPYING" = "$dir\libogg-COPYING" }
}
Write-Output $dll
