# Builds SunMoon.exe with PyInstaller and installs it to C:\Radio\sunmoon,
# with Desktop and Start menu shortcuts. Keeps the sunmoon.json already there.
#
#   pwsh -File build.ps1              build and install
#   pwsh -File build.ps1 -NoInstall   build only
#
# The build venv and PyInstaller's work files live in %LOCALAPPDATA%\sunmoon-build,
# not in this OneDrive folder, so OneDrive isn't syncing thousands of build files.
# Keep this file pure ASCII: Windows PowerShell 5.1 reads it as CP1252.

param(
    [string]$InstallDir = "C:\Radio\sunmoon",
    [switch]$NoInstall
)
$ErrorActionPreference = "Stop"

$repo = $PSScriptRoot
$work = Join-Path $env:LOCALAPPDATA "sunmoon-build"
$venv = Join-Path $work "venv"
$py = Join-Path $venv "Scripts\python.exe"

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
if ($NoInstall) { return }

if (Get-Process SunMoon -ErrorAction SilentlyContinue) {
    throw "SunMoon is running. Close it, then build again."
}

New-Item -ItemType Directory -Force $InstallDir | Out-Null
$internal = Join-Path $InstallDir "_internal"
if (Test-Path $internal) { Remove-Item $internal -Recurse -Force }
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
