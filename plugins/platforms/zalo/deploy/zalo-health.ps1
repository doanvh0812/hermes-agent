# Zalo bridge health check - Windows port of deploy/zalo-health.sh
#
# Catches BOTH silent failure modes, neither of which surfaces anywhere else
# (the gateway keeps reporting "connected" through both):
#   1. bridge dead / not logged in  -> /health not ready
#   2. bridge token drift           -> /events returns "bad bridge token"
#
# Scheduled every 10 minutes by ops\register-tasks.ps1.
#
# Differences from the bash original, forced by the platform:
#   - `logger` (syslog) does not exist. Writes to the Windows Application
#     event log under source "ZaloBridge" plus a plain-text log file.
#   - Alerts fire only on TRANSITION into failure, same as the original, so a
#     bridge that stays broken does not emit a line every 10 minutes forever.

$ErrorActionPreference = 'Stop'

$ProfileHome = 'C:\Users\USER\AppData\Local\hermes\profiles\zalo-bot'
$EnvFile     = Join-Path $ProfileHome '.env'
$StateFile   = Join-Path $ProfileHome 'zalo\.health-state'
$LogFile     = Join-Path $ProfileHome 'logs\zalo-health.log'
$EventSource = 'ZaloBridge'

# --- read config out of the profile .env -------------------------------------
function Get-EnvValue([string]$Name) {
    if (-not (Test-Path $EnvFile)) { return $null }
    $line = Select-String -Path $EnvFile -Pattern "^$Name=" -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if (-not $line) { return $null }
    $v = ($line.Line -split '=', 2)[1]
    if ([string]::IsNullOrWhiteSpace($v)) { return $null }
    return $v.Trim()
}

$Port  = Get-EnvValue 'ZALO_BRIDGE_PORT'; if (-not $Port) { $Port = '8647' }
$Token = Get-EnvValue 'ZALO_BRIDGE_TOKEN'

New-Item -ItemType Directory -Force -Path (Split-Path $StateFile) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $LogFile)   | Out-Null

function Write-Alert([string]$Level, [string]$Message) {
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Add-Content -Path $LogFile -Value "$stamp [$Level] $Message" -Encoding utf8
    try {
        if (-not [System.Diagnostics.EventLog]::SourceExists($EventSource)) {
            New-EventLog -LogName Application -Source $EventSource
        }
        $type = if ($Level -eq 'ALERT') { 'Error' } else { 'Information' }
        Write-EventLog -LogName Application -Source $EventSource -EventId 9001 `
                       -EntryType $type -Message "zalo-bridge: $Message"
    } catch {
        # Event log needs an elevated first write to create the source. The
        # file log above still captured it, so do not fail the check.
    }
    # TODO: route somewhere a person actually sees. Known gap #9 in DEPLOY.md:
    # an alert nobody reads is not monitoring. e.g.
    #   Invoke-RestMethod -Method Post -Uri 'https://ntfy.sh/<topic>' -Body $Message
}

function Fail([string]$Reason) {
    $prev = if (Test-Path $StateFile) { (Get-Content $StateFile -Raw).Trim() } else { '' }
    Set-Content -Path $StateFile -Value "fail:$Reason" -Encoding utf8
    if ($prev -eq "fail:$Reason") { exit 1 }   # already alerted on this state
    Write-Alert 'ALERT' $Reason
    exit 1
}

# --- 0. the gateway itself ----------------------------------------------------
# Added from the upstream check list: a healthy bridge proves nothing if no
# gateway is polling it. Inbound messages stop while /send still works and the
# cached status keeps reporting "connected".
#
# The Linux script checks `systemctl is-active`. Here the equivalent evidence is
# the scheduled task state plus a live pid in the profile's gateway_state.json -
# and the file's LOCATION also proves which profile that gateway resolved.
$task = Get-ScheduledTask -TaskName 'HermesZaloGateway' -ErrorAction SilentlyContinue
if (-not $task) {
    Fail 'scheduled task HermesZaloGateway is not registered'
} elseif ($task.State -ne 'Running') {
    Fail "gateway task is '$($task.State)', not Running"
}

# $GwStateFile, NOT $stateFile: PowerShell variable names are case-insensitive,
# so `$stateFile` IS `$StateFile` -- the health-state path set at the top. With
# that collision every later write went into the gateway's own status file:
# Fail() stamped "fail:<reason>" over it and the OK path stamped "ok".
#
# The damage was self-sustaining. Once this file held plain text, the next run's
# ConvertFrom-Json threw, which Fail'd as 'gateway_state.json is unreadable' --
# the check diagnosing its own corruption -- and that Fail is a step-0 exit, so
# steps 1 and 2 NEVER RAN. The bridge died 2026-10-28 ~17:00 and nothing
# reported it for 25 hours: the monitor had been failing on its own damage
# since 10-26 and latches silently while the reason is unchanged.
# gateway_state.json is also what `hermes update` reads to verify fleet
# versions, so the blast radius was wider than this script.
$GwStateFile = Join-Path $ProfileHome 'gateway_state.json'
if (Test-Path $GwStateFile) {
    # The gateway rewrites this file on every runtime-status write, so a read
    # can catch a torn write. Retry once before calling it broken -- a
    # transient parse failure is not worth paging anyone over, and treating it
    # as fatal is what kept step 1 unreachable.
    $gw = $null
    foreach ($attempt in 1, 2) {
        try {
            # -Encoding utf8 so a BOM does not read as a JSON primitive.
            $gw = Get-Content $GwStateFile -Raw -Encoding utf8 | ConvertFrom-Json
            break
        } catch {
            if ($attempt -eq 2) { Fail 'gateway_state.json is unreadable' }
            Start-Sleep -Milliseconds 400
        }
    }
    if ($gw -and -not (Get-Process -Id $gw.pid -ErrorAction SilentlyContinue)) {
        Fail "gateway pid $($gw.pid) is dead but the task still shows Running"
    }
} else {
    Fail 'no gateway_state.json under the zalo-bot profile - wrong profile or never started'
}

# --- 1. bridge reachable and logged in ---------------------------------------
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
} catch {
    Fail "bridge unreachable on port $Port"
}

if (-not $health.ready) {
    Fail 'bridge up but not logged in (re-run: node index.js --qr-login)'
}

# --- 2. token drift -----------------------------------------------------------
if ($Token) {
    try {
        $events = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/events?since=0" `
                                    -Headers @{ 'X-Bridge-Token' = $Token } `
                                    -TimeoutSec 5 -UseBasicParsing
        if ($events.Content -match 'bad bridge token') {
            Fail 'token drift - adapter cannot read events (set ZALO_BRIDGE_TOKEN)'
        }
    } catch {
        Fail 'token drift - /events rejected the configured bridge token'
    }
} else {
    Write-Alert 'WARN' 'ZALO_BRIDGE_TOKEN unset; token drift will go undetected'
}

# --- 3. recovered? ------------------------------------------------------------
if (Test-Path $StateFile) {
    $prev = (Get-Content $StateFile -Raw).Trim()
    if ($prev -like 'fail:*') { Write-Alert 'RECOVERED' 'bridge healthy again' }
}
Set-Content -Path $StateFile -Value 'ok' -Encoding utf8
exit 0
