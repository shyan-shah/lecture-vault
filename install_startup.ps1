# Starts Lecture Vault quietly every time you log in to Windows, and adds a desktop shortcut.
# Run once:  powershell -ExecutionPolicy Bypass -File install_startup.ps1
# Remove:    delete "Lecture Vault.lnk" from shell:startup
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $here ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $py)) { $py = (Get-Command pythonw -ErrorAction Stop).Source }
$shell = New-Object -ComObject WScript.Shell

$startup = Join-Path ([Environment]::GetFolderPath("Startup")) "Lecture Vault.lnk"
$lnk = $shell.CreateShortcut($startup)
$lnk.TargetPath = $py
$lnk.Arguments = "`"$here\app.py`" --no-browser"
$lnk.WorkingDirectory = $here
$lnk.WindowStyle = 7
$lnk.Description = "Lecture Vault background service"
$lnk.Save()

$desk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Lecture Vault.lnk"
$lnk = $shell.CreateShortcut($desk)
$lnk.TargetPath = $py
$lnk.Arguments = "`"$here\app.py`""
$lnk.WorkingDirectory = $here
$lnk.Description = "Open Lecture Vault"
$lnk.Save()

Write-Host "Lecture Vault will start in the background at login. Desktop shortcut added."
