# Register the Zalo admin panel as a scheduled task.
#
# Mirrors ops\register-tasks.ps1: at-startup trigger (no interactive logon
# needed, which is what loginctl enable-linger buys you on systemd), S4U logon
# so no password is stored, and a restart policy.
#
# The panel binds 127.0.0.1 only. Reaching it from a browser elsewhere is the
# reverse proxy's job — see Caddyfile.example.
#
# Idempotent. Run from an elevated PowerShell.

$ErrorActionPreference = 'Stop'

$HermesRoot  = 'C:\Users\Administrator\AppData\Local\hermes'
$ProfileHome = Join-Path $HermesRoot 'profiles\zalo-bot'
$AppDir      = Join-Path $HermesRoot 'hermes-agent\plugins\platforms\zalo\admin'
$Runner      = Join-Path $ProfileHome 'ops\run-admin.ps1'
$TaskName    = 'HermesZaloAdmin'

New-Item -ItemType Directory -Force -Path (Split-Path $Runner) | Out-Null

# The runner exists for the same reason run-gateway.ps1 does: Windows
# PowerShell 5.1 wraps a native process's stderr in NativeCommandError, which
# under $ErrorActionPreference='Stop' turns the first harmless warning into a
# fatal error. Start-Process redirects at the OS level and sidesteps it.
@"
`$ErrorActionPreference = 'Stop'

`$HermesRoot  = '$HermesRoot'
`$ProfileHome = '$ProfileHome'
`$env:HERMES_HOME = `$ProfileHome
`$env:PATH = "`$HermesRoot\bin;`$env:PATH"

`$logDir = Join-Path `$ProfileHome 'logs'
New-Item -ItemType Directory -Force -Path `$logDir | Out-Null
`$out = Join-Path `$logDir 'admin.log'
`$err = Join-Path `$logDir 'admin.err.log'
foreach (`$f in @(`$out, `$err)) {
    if (Test-Path `$f) { Move-Item `$f "`$f.prev" -Force -ErrorAction SilentlyContinue }
}

`$proc = Start-Process ``
    -FilePath (Join-Path `$HermesRoot 'hermes-agent\venv\Scripts\python.exe') ``
    -ArgumentList '$AppDir\server.py' ``
    -WorkingDirectory '$AppDir' ``
    -RedirectStandardOutput `$out -RedirectStandardError `$err ``
    -NoNewWindow -PassThru
try { `$proc.WaitForExit(); exit `$proc.ExitCode }
finally { if (-not `$proc.HasExited) { Stop-Process -Id `$proc.Id -Force -ErrorAction SilentlyContinue } }
"@ | Set-Content -Path $Runner -Encoding utf8

$action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Runner`""

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$settings.ExecutionTimeLimit = 'PT0S'     # a daemon, not a batch job
$settings.RestartInterval    = 'PT1M'
$settings.RestartCount       = 999

$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\Administrator" `
    -LogonType S4U -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $settings `
    -Principal $principal `
    -Description 'Zalo bot admin panel on 127.0.0.1:8648. Put a TLS reverse proxy in front of it; it refuses to bind anything else.' | Out-Null

Write-Output "registered: $TaskName"
Write-Output "runner    : $Runner"
Write-Output ''
Write-Output 'Chua dat mat khau thi chay truoc:'
Write-Output "  `$env:HERMES_HOME='$ProfileHome'"
Write-Output "  & '$HermesRoot\hermes-agent\venv\Scripts\python.exe' '$AppDir\server.py' --set-password"
