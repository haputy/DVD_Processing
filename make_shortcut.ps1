# Creates a Desktop shortcut "DVD to Jellyfin" that opens the window through launch.bat.
# Run it once:  powershell -ExecutionPolicy Bypass -File make_shortcut.ps1
# The Desktop folder is looked up, since it may be inside OneDrive.

$repo = "C:\github\DVD_Processing"
$desktop = [Environment]::GetFolderPath('Desktop')
$path = Join-Path $desktop "DVD to Jellyfin.lnk"

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($path)
$link.TargetPath = Join-Path $repo "launch.bat"
$link.WorkingDirectory = $repo
$link.WindowStyle = 7    # start minimised, so the batch file's console does not flash up
$link.IconLocation = "$env:SystemRoot\System32\shell32.dll,188"   # an optical disc
$link.Description = "Rip TV DVDs and put the episodes on Jellyfin"
$link.Save()

Write-Host "Shortcut created: $path"
