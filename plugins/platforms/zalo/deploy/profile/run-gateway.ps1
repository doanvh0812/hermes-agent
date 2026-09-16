# Gateway process bound to the zalo-bot profile.
# Windows replacement for deploy/profile/hermes-gateway-zalo.service.
#
# A gateway serves exactly ONE profile, resolved from HERMES_HOME at startup.
# HERMES_HOME whose immediate parent directory is named "profiles" selects that
# profile; pointing it at the hermes root runs the default profile instead.
# This is the whole mechanism - there is no per-platform routing, and
# `gateway.routing` in config.yaml is not a real key (it is silently ignored).
#
# STREAM HANDLING - do not "simplify" this back to `python ... *>&1 | Tee-Object`.
# Windows PowerShell 5.1 wraps every stderr line from a native executable in a
# NativeCommandError ErrorRecord. With $ErrorActionPreference = 'Stop' that
# makes the FIRST stderr line terminating - and Hermes writes a harmless
# SQLite/WAL warning to stderr during startup, so the wrapper killed a
# perfectly healthy gateway and the task reported exit code 1 with no log.
# Start-Process redirects at the OS level, so PowerShell never touches the
# streams and a warning stays a warning.

$ErrorActionPreference = 'Stop'

$HermesRoot  = 'C:\Users\Administrator\AppData\Local\hermes'
$ProfileHome = Join-Path $HermesRoot 'profiles\zalo-bot'

# THE line that selects the locked-down profile. If this is wrong the gateway
# runs the ROOT profile - which has a terminal - and every restriction in the
# zalo-bot config.yaml is bypassed while everything still looks healthy.
$env:HERMES_HOME = $ProfileHome
$env:PATH = "$HermesRoot\bin;$env:PATH"

$logDir = Join-Path $ProfileHome 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Start-Process truncates these on each launch, so keep one previous
# generation - a crash loop otherwise erases the evidence of why it crashed.
$out = Join-Path $logDir 'gateway.log'
$err = Join-Path $logDir 'gateway.err.log'
foreach ($f in @($out, $err)) {
    if (Test-Path $f) { Move-Item $f "$f.prev" -Force -ErrorAction SilentlyContinue }
}

# --replace: Start-Process detaches the python child from the job object Task
# Scheduler kills, so `Stop-ScheduledTask` leaves the gateway running as an
# orphan and the NEXT start aborts with "Another gateway instance is already
# running (PID ...)". --replace makes the incoming process evict the stale one
# instead of refusing to boot. Hermes uses the same flag internally.
$proc = Start-Process `
    -FilePath (Join-Path $HermesRoot 'hermes-agent\venv\Scripts\python.exe') `
    -ArgumentList '-m', 'hermes_cli.main', 'gateway', 'run', '--replace' `
    -WorkingDirectory (Join-Path $HermesRoot 'hermes-agent') `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err `
    -NoNewWindow -PassThru

# LOAD-BEARING. Start-Process -PassThru hands back a Process object whose
# ExitCode stays $null unless the handle is cached first, so `exit $p.ExitCode`
# below exits 0 no matter how the gateway died. That is not theoretical: on
# 16/09/2026 the shutdown watchdog exited the gateway with code 75 ("restart
# me"), this wrapper reported success, Task Scheduler's RestartCount=999 never
# fired, and the bot was silent for seven hours while every indicator looked
# fine — task State Ready, LastTaskResult 0, no error anywhere.
# Touching .Handle populates ExitCode. Verified: without it 75 arrives as 0.
$null = $proc.Handle

try {
    # Hand the child's exit status back to Task Scheduler so its restart policy
    # (RestartCount/RestartInterval) sees a real failure.
    $proc.WaitForExit()
    exit $proc.ExitCode
}
finally {
    # Covers a graceful stop of the wrapper. A hard kill skips this, which is
    # why --replace above is the load-bearing half of the fix.
    if (-not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}
