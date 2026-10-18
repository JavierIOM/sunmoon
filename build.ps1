# Builds SunMoon.exe with PyInstaller and installs it to C:\Radio\sunmoon,
# with Desktop and Start menu shortcuts. Keeps the sunmoon.json already there.
#
#   pwsh -File build.ps1              build, zip a release and install
#   pwsh -File build.ps1 -NoInstall   build and zip only
#
# The release zip lands in %LOCALAPPDATA%\sunmoon-build\release, next to the hamlib
# source tarball that has to be attached to the GitHub release with it (GPL).
#
# rotctl.exe and its DLLs come from the official hamlib-w64 zip, downloaded once and
# checked against the SHA-256 below. To move to a new hamlib, change all three lines.
#
# The build venv and PyInstaller's work files live in %LOCALAPPDATA%\sunmoon-build,
# not in this OneDrive folder, so OneDrive isn't syncing thousands of build files.
# Keep this file pure ASCII: Windows PowerShell 5.1 reads it as CP1252.

param(
    [string]$InstallDir = "C:\Radio\sunmoon",
    [switch]$NoInstall
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$hamlibVersion = "4.7.0"
$hamlibZipSha = "f8854ba06be5eac851113fd849b0737ae77e02ea78da246b469483e18b6483e0"
$hamlibSrcSha = "24542b09cb2432458ba239b2ba8f5b7fb67cde64df6553f150e6eb8475a87a23"

$repo = $PSScriptRoot
$work = Join-Path $env:LOCALAPPDATA "sunmoon-build"
$venv = Join-Path $work "venv"
$py = Join-Path $venv "Scripts\python.exe"

# hamlib: the Windows binaries we ship, and the source the GPL says must go with them
$hamlibDir = Join-Path $work "hamlib"
New-Item -ItemType Directory -Force $hamlibDir | Out-Null
$hamlibUrl = "https://github.com/Hamlib/Hamlib/releases/download/$hamlibVersion"
$hamlibZip = Join-Path $hamlibDir "hamlib-w64-$hamlibVersion.zip"
$hamlibSrc = Join-Path $hamlibDir "hamlib-$hamlibVersion.tar.gz"
foreach ($f in @(@($hamlibZip, $hamlibZipSha), @($hamlibSrc, $hamlibSrcSha))) {
    $file, $sha = $f
    if (-not (Test-Path $file)) {
        Write-Host "Downloading $(Split-Path $file -Leaf)"
        Invoke-WebRequest "$hamlibUrl/$(Split-Path $file -Leaf)" -OutFile $file
    }
    if ((Get-FileHash $file -Algorithm SHA256).Hash -ne $sha) {
        Remove-Item $file -Force
        throw "$(Split-Path $file -Leaf) doesn't match its SHA-256, deleted. Build again to re-download."
    }
}
$hamlibOut = Join-Path $hamlibDir "x"
if (Test-Path $hamlibOut) { Remove-Item $hamlibOut -Recurse -Force }
Expand-Archive $hamlibZip -DestinationPath $hamlibOut
$hamlibTree = Join-Path $hamlibOut "hamlib-w64-$hamlibVersion"

if (-not (Test-Path $py)) {
    Write-Host "Creating build venv in $venv"
    python -m venv $venv
    & $py -m pip install --disable-pip-version-check -q pyinstaller pillow
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

& $py (Join-Path $repo "assets\make_icon.py")
if ($LASTEXITCODE -ne 0) { throw "icon build failed" }

$ico = Join-Path $repo "assets\sunmoon.ico"
& $py -m PyInstaller --noconfirm --clean --windowed --name SunMoon `
    --icon $ico --add-data "$ico;assets" `
    --distpath (Join-Path $work "dist") --workpath (Join-Path $work "build") --specpath $work `
    (Join-Path $repo "sunmoon_gui.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$built = Join-Path $work "dist\SunMoon"
Write-Host "Built $built"

# rotctl.exe, every DLL from hamlib's bin folder, and its licence files, unmodified.
# find_rotctl picks this up through its hamlib*\bin\rotctl.exe search next to the exe.
$bundled = Join-Path $built "hamlib-w64-$hamlibVersion"
New-Item -ItemType Directory -Force (Join-Path $bundled "bin") | Out-Null
Copy-Item (Join-Path $hamlibTree "bin\rotctl.exe") (Join-Path $bundled "bin")
Copy-Item (Join-Path $hamlibTree "bin\*.dll") (Join-Path $bundled "bin")
Copy-Item (Join-Path $hamlibTree "*.txt") $bundled
@"
This folder is rotctl.exe and the DLLs it needs, unmodified, from the official
Hamlib $hamlibVersion Windows build (hamlib-w64-$hamlibVersion.zip, SHA-256
$hamlibZipSha).

Hamlib is free software, not part of SunMoon and not covered by SunMoon's MIT
licence. The library is under the LGPL and the programs under the GPL: see
LICENSE.txt, COPYING.txt and COPYING.LIB.txt here.

Its complete source code, hamlib-$hamlibVersion.tar.gz, is attached to every
SunMoon release that includes it:
    https://github.com/JavierIOM/sunmoon/releases
and is also at
    https://github.com/Hamlib/Hamlib/releases/tag/$hamlibVersion
"@ | Set-Content (Join-Path $bundled "SOURCE.txt") -Encoding ascii
Write-Host "Added hamlib $hamlibVersion"

# release zip, named from the version in sunmoon.py, with the program in a SunMoon folder
$version = [regex]::Match((Get-Content (Join-Path $repo "sunmoon.py") -Raw), '__version__ = "([^"]+)"').Groups[1].Value
$release = Join-Path $work "release"
New-Item -ItemType Directory -Force $release | Out-Null
$zip = Join-Path $release "SunMoon-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $built -DestinationPath $zip
Copy-Item $hamlibSrc $release -Force
Write-Host "Zipped $zip, hamlib source alongside"
if ($NoInstall) { return }

if (Get-Process SunMoon -ErrorAction SilentlyContinue) {
    throw "SunMoon is running. Close it, then build again."
}

New-Item -ItemType Directory -Force $InstallDir | Out-Null
$internal = Join-Path $InstallDir "_internal"
if (Test-Path $internal) { Remove-Item $internal -Recurse -Force }
Get-ChildItem $InstallDir -Directory -Filter "hamlib-w64-*" | Remove-Item -Recurse -Force
Copy-Item (Join-Path $built "*") $InstallDir -Recurse -Force

$exe = Join-Path $InstallDir "SunMoon.exe"
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "SunMoon.lnk"))
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = $InstallDir
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = "Point the antenna at the sun or moon to check rotator calibration"
    $lnk.Save()
}
Write-Host "Installed $exe, shortcuts on the Desktop and in the Start menu"
