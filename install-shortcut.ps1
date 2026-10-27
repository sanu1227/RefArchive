param([string]$Destination = [Environment]::GetFolderPath('Desktop'), [string]$Target = (Join-Path $PSScriptRoot 'start.cmd'))
$ErrorActionPreference = 'Stop'
$taskTarget = $Target
$taskShortcutPath = Join-Path $Destination 'RefArchive.lnk'
$taskShell = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $taskShortcutPath) {
    $taskExisting = $taskShell.CreateShortcut($taskShortcutPath)
    if ($taskExisting.TargetPath -ne $taskTarget) { throw 'A different RefArchive shortcut already exists.' }
    Write-Output $taskShortcutPath
    exit 0
}
$taskShortcut = $taskShell.CreateShortcut($taskShortcutPath)
$taskShortcut.TargetPath = $taskTarget
$taskShortcut.WorkingDirectory = Split-Path -Parent $taskTarget
$taskShortcut.WindowStyle = 7
$taskShortcut.Description = 'RefArchive local archive'
$taskShortcut.Save()
Write-Output $taskShortcutPath
