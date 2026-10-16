<#
.SYNOPSIS
    Register the dzsm daemon as a scheduled task that starts with the machine.

.DESCRIPTION
    Run from an elevated PowerShell. Defaults to running as SYSTEM, which needs
    no password and no interactive logon — the two things that make an
    "at startup" task awkward to register.

    Pass -AsCurrentUser to run as you instead, using S4U (Service For User):
    also passwordless, but the account has no access to network shares or
    mapped drives. Fine for a local server, not if your mods live on a NAS.

.EXAMPLE
    .\install-task.ps1
    .\install-task.ps1 -AsCurrentUser
    .\install-task.ps1 -Launcher "D:\dzsm\dzsm-daemon.cmd"
#>
[CmdletBinding()]
param(
    # The wrapper script, not dzsm.exe directly: it is what sets the
    # environment deterministically. See dzsm-daemon.cmd for why.
    [string] $Launcher = "D:\dzsm\dzsm-daemon.cmd",

    [string] $TaskName = "dzsm",

    # Run as the calling user via S4U instead of SYSTEM. Slightly less
    # privileged, which is the better default if your mods are on local disk
    # and you would rather the DayZ servers not run as SYSTEM.
    [switch] $AsCurrentUser
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Launcher)) {
    throw "launcher not found: $Launcher. Copy deploy\dzsm-daemon.cmd there and edit the paths at the top of it first."
}

$home_dir = Split-Path -Parent $Launcher

$action = New-ScheduledTaskAction -Execute $Launcher -WorkingDirectory $home_dir
$trigger = New-ScheduledTaskTrigger -AtStartup

# ExecutionTimeLimit of zero is the important one. Task Scheduler's default is
# "stop the task if it runs longer than 3 days", which would quietly kill the
# daemon every 72 hours and is the most common reason a startup daemon seems to
# die on its own.
#
# MultipleInstances IgnoreNew so a manual "Run" while it is already up does not
# start a second daemon fighting the first over state.json.
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

if ($AsCurrentUser) {
    $who = "$env:USERDOMAIN\$env:USERNAME"
    # S4U: runs as this user, logged on or not, with no stored password.
    $principal = New-ScheduledTaskPrincipal -UserId $who -LogonType S4U -RunLevel Highest
} else {
    # SYSTEM needs no password and exists before any logon, so the task can
    # genuinely run "at startup" on an unattended box.
    $who = "NT AUTHORITY\SYSTEM"
    $principal = New-ScheduledTaskPrincipal -UserId $who -LogonType ServiceAccount -RunLevel Highest
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal | Out-Null

Write-Host "registered '$TaskName' running as $who" -ForegroundColor Green
Write-Host "  action:  $Launcher"
Write-Host "  working: $home_dir"

# Read the limit back rather than trusting that it was set: this is the setting
# that matters most and the one most likely to be silently wrong.
$limit = (Get-ScheduledTask -TaskName $TaskName).Settings.ExecutionTimeLimit
if ($limit -and $limit -ne "PT0S") {
    Write-Warning "execution time limit is '$limit', not unlimited. Open the task in Task Scheduler, Settings tab, and uncheck 'Stop the task if it runs longer than' or the daemon will be killed after that long."
} else {
    Write-Host "  time limit: none (correct for a daemon)"
}

Write-Host ""
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check it with:      Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "A LastTaskResult of 267009 means 'currently running', which is what you want."
