#Requires -Version 5.1
<#
  LKQ Launch automation — install on a Windows PC that has the tablet.

  Right-click install-on-pc.bat  →  Run
  or in PowerShell:

    powershell -ExecutionPolicy Bypass -File .\install-on-pc.ps1
#>
$ErrorActionPreference = "Stop"

$RepoUrl   = "https://github.com/LKQSUP/Launch_automation.git"
$InstallRoot = Join-Path $env:USERPROFILE "Desktop\LKQ_dev"
$AppDir    = Join-Path $InstallRoot "Launch_automation"
$LogFile   = Join-Path $env:TEMP "lkq-launch-install.log"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
    Add-Content -Path $LogFile -Value "$(Get-Date -Format o)  $Message"
}

function Write-Ok([string]$Message) {
    Write-Host "    $Message" -ForegroundColor Green
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Add-UserPath([string]$Dir) {
    if (-not (Test-Path $Dir)) { return }
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($user) { $parts = $user -split ";" | Where-Object { $_ } }
    if ($parts -notcontains $Dir) {
        [Environment]::SetEnvironmentVariable("Path", (@($parts + $Dir) -join ";"), "User")
    }
    if ($env:Path -notlike "*$Dir*") {
        $env:Path = "$Dir;$env:Path"
    }
}

function Test-Cmd([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Ensure-Winget {
    if (Test-Cmd "winget") { return }
    throw "winget is not available. Install 'App Installer' from the Microsoft Store, then run this script again."
}

function Install-WingetId([string]$Id) {
    Write-Step "Installing $Id (skipped if already present)"
    $list = winget list --id $Id -e --accept-source-agreements 2>$null
    if ($LASTEXITCODE -eq 0 -and "$list" -match [regex]::Escape($Id)) {
        Write-Ok "$Id already installed"
        return
    }
    winget install --id $Id -e --accept-source-agreements --accept-package-agreements --disable-interactivity
    Refresh-Path
    Write-Ok "$Id ready"
}

function Find-Python {
    Refresh-Path
    $candidates = @(
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "${env:ProgramFiles(x86)}\Python311\python.exe"
    ) | Where-Object { $_ -and (Test-Path $_) }
    foreach ($exe in $candidates) {
        if ($exe -like "*\py.exe") { continue }
        return $exe
    }
    if (Test-Cmd "py") {
        return "py -3.11"
    }
    return $null
}

function Ensure-Python {
    $py = Find-Python
    if (-not $py) {
        Install-WingetId "Python.Python.3.11"
        Refresh-Path
        $py = Find-Python
    }
    if (-not $py) {
        throw "Python was not found after install. Close this window, open a new PowerShell, and run the script again."
    }
    Write-Ok "Python: $py"
    return $py
}

function Ensure-Git {
    if (-not (Test-Cmd "git")) {
        Install-WingetId "Git.Git"
        Refresh-Path
        Add-UserPath "C:\Program Files\Git\cmd"
        Refresh-Path
    }
    if (-not (Test-Cmd "git")) {
        throw "Git was not found after install. Open a new PowerShell and run the script again."
    }
    Write-Ok "Git: $(git --version)"
}

function Ensure-Adb {
    if (Test-Cmd "adb") {
        Write-Ok "adb already on PATH"
        return
    }
    try {
        Install-WingetId "Google.PlatformTools"
        Refresh-Path
    } catch {
        Write-Host "    winget platform-tools failed, downloading zip instead..." -ForegroundColor Yellow
    }
    if (Test-Cmd "adb") { Write-Ok "adb ready"; return }

    $toolsRoot = Join-Path $env:LOCALAPPDATA "Android"
    $zip = Join-Path $env:TEMP "platform-tools-windows.zip"
    $url = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
    Write-Step "Downloading Android platform-tools"
    New-Item -ItemType Directory -Force -Path $toolsRoot | Out-Null
    Invoke-WebRequest -Uri $url -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $toolsRoot -Force
    $adbDir = Join-Path $toolsRoot "platform-tools"
    Add-UserPath $adbDir
    Refresh-Path
    if (-not (Test-Path (Join-Path $adbDir "adb.exe"))) {
        throw "adb.exe missing after download."
    }
    Write-Ok "adb installed to $adbDir"
}

function Ensure-Scrcpy {
    if (Test-Cmd "scrcpy") {
        Write-Ok "scrcpy already on PATH"
        return
    }
    try {
        Install-WingetId "Genymobile.scrcpy"
        Refresh-Path
    } catch {
        Write-Host "    scrcpy install skipped (Show screen will be unavailable until it is installed)." -ForegroundColor Yellow
        return
    }
    if (Test-Cmd "scrcpy") { Write-Ok "scrcpy ready" }
}

function Get-Repo {
    Write-Step "Get code from GitHub"
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    if (Test-Path (Join-Path $AppDir ".git")) {
        Push-Location $AppDir
        git pull --ff-only
        Pop-Location
        Write-Ok "Updated existing folder: $AppDir"
        return
    }
    if (Test-Path $AppDir) {
        throw "$AppDir already exists but is not a git clone. Move or rename it, then run again."
    }
    git clone $RepoUrl $AppDir
    Write-Ok "Cloned to $AppDir"
}

function Install-PythonDeps([string]$PythonExe) {
    Write-Step "Python packages (first time can take 10–20 minutes)"
    Push-Location $AppDir
    try {
        $venvPy = Join-Path $AppDir ".venv\Scripts\python.exe"
        if (-not (Test-Path $venvPy)) {
            if ($PythonExe -eq "py -3.11") {
                & py -3.11 -m venv .venv
            } else {
                & $PythonExe -m venv .venv
            }
        }
        $venvPy = Join-Path $AppDir ".venv\Scripts\python.exe"
        if (-not (Test-Path $venvPy)) {
            throw "Virtual environment was not created."
        }
        & $venvPy -m pip install --upgrade pip
        & $venvPy -m pip install -r (Join-Path $AppDir "requirements.txt")
        Write-Ok "Python packages installed"
    } finally {
        Pop-Location
    }
}

function Write-StartScripts {
    $startBat = Join-Path $AppDir "start-app.bat"
    @"
@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
  echo Run install-on-pc.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo Starting Launch X431 app...
streamlit run main.py
pause
"@ | Set-Content -Path $startBat -Encoding ASCII

    $desktop = [Environment]::GetFolderPath("Desktop")
    $lnk = Join-Path $desktop "Launch X431.lnk"
    $w = New-Object -ComObject WScript.Shell
    $s = $w.CreateShortcut($lnk)
    $s.TargetPath = $startBat
    $s.WorkingDirectory = $AppDir
    $s.WindowStyle = 1
    $s.Description = "LKQ Launch X431 automation"
    $s.Save()
    Write-Ok "Desktop shortcut: Launch X431.lnk"
    Write-Ok "Start script: $startBat"
}

# --- run --------------------------------------------------------------------
try {
    "LKQ Launch install started" | Set-Content $LogFile
    Write-Host "Install log: $LogFile" -ForegroundColor DarkGray

    Ensure-Winget
    Ensure-Git
    $python = Ensure-Python
    Ensure-Adb
    Ensure-Scrcpy
    Get-Repo
    Install-PythonDeps $python
    Write-StartScripts

    Write-Host ""
    Write-Host "Install finished." -ForegroundColor Green
    Write-Host "1. Plug in the Launch tablet (USB debugging on)."
    Write-Host "2. Double-click  Launch X431  on the Desktop."
    Write-Host "3. In the browser: Sync ADB → pick tablet → start."
    Write-Host ""
    Write-Host "Later updates on this PC:"
    Write-Host "    cd `"$AppDir`""
    Write-Host "    git pull"
    Write-Host "    .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    Write-Host "    then start the app again."
} catch {
    Write-Host ""
    Write-Host "INSTALL FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Log: $LogFile"
    exit 1
}
