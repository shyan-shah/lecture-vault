# Lecture Vault installer for Windows. From this folder, in PowerShell:
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Safe to run again (it updates packages and re-opens the setup wizard).
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

function Find-Python {
    foreach ($c in @("py -3", "python", "python3")) {
        try {
            $parts = $c.Split(" ")
            $v = & $parts[0] $parts[1..9] -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $maj, $min = $v.Split(".") | ForEach-Object { [int]$_ }
                if ($maj -eq 3 -and $min -ge 11) { return $c }
            }
        } catch {}
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." -ForegroundColor Yellow
    exit 1
}
Write-Host "Using $py"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    $parts = $py.Split(" "); & $parts[0] $parts[1..9] -m venv .venv
}
$vpy = Join-Path $here ".venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip --quiet
Write-Host "Installing packages (a minute or two)..."
& $vpy -m pip install -r requirements.txt --quiet
# Uses Microsoft Edge for the university sign-in, so no separate browser download is needed.

$whisper = Read-Host "Install local transcription for lectures Panopto never captions? Adds ~1 GB later on first use. [y/N]"
if ($whisper -match "^[yY]") { & $vpy -m pip install -r requirements-whisper.txt --quiet }

Write-Host ""
& $vpy setup_wizard.py

$auto = Read-Host "Start Lecture Vault automatically when you log in to Windows (needed for automatic notes)? [Y/n]"
if ($auto -notmatch "^[nN]") { & powershell -ExecutionPolicy Bypass -File (Join-Path $here "install_startup.ps1") }

Write-Host ""
Write-Host "Done. Open Lecture Vault from the desktop shortcut (or start.bat) and press 'Sign in'." -ForegroundColor Green
