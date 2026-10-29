<#
  Bundle the Zalo bot's live state out of Windows, for restore on Linux.

  WHAT GOES IN
    Everything that cannot be rebuilt: the Zalo session, the allowlist, the
    dedup cursor, the audit log, the attachments, the conversation history
    (state.db), the skills, the memories, the profile config and secrets.

  WHAT STAYS OUT
    Anything a fresh install regenerates: venv, node_modules, __pycache__,
    model/provider caches, sandboxes, terminal sessions, .bak copies, and the
    runtime files that describe THIS host (gateway.pid, gateway_state.json,
    .health-state) -- carrying those over makes the new host diagnose the old
    one's state.

  TWO MODES
    (default)  live snapshot, services keep running. Use it to rehearse the
               whole restore on Linux days before cutover.
    -Final     stops the gateway + bridge first, so the bundle is the last
               word. The bot is DOWN from here until the Linux side is up.

  state.db is copied through sqlite3's backup API, not Copy-Item: the gateway
  writes to it in WAL mode, and a plain file copy of a live WAL database is a
  torn snapshot that may be missing the newest turns -- exactly the history
  this migration exists to preserve.
#>
[CmdletBinding()]
param(
    [string]$ProfileHome = "$env:LOCALAPPDATA\hermes\profiles\zalo-bot",
    [string]$AgentDir    = "$env:LOCALAPPDATA\hermes\hermes-agent",
    [string]$ForkDir     = "$env:USERPROFILE\vni_zalo_agent",
    [string]$OutDir      = "$env:USERPROFILE\zalo-migrate",
    [switch]$Final,
    [switch]$NoAttachments
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Host $m }
function Die($m) { Write-Host "FATAL: $m" -ForegroundColor Red; exit 1 }

foreach ($d in @($ProfileHome, $AgentDir, $ForkDir)) {
    if (-not (Test-Path $d)) { Die "not found: $d" }
}

$stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stage   = Join-Path $OutDir "stage-$stamp"
$profOut = Join-Path $stage 'profile'
New-Item -ItemType Directory -Force -Path $profOut | Out-Null

# ---------------------------------------------------------------- 1. quiesce
if ($Final) {
    Say "== FINAL bundle: stopping the bot =="
    foreach ($t in 'HermesZaloHealth', 'HermesZaloGateway') {
        schtasks /End /TN $t 2>&1 | Out-Null
        schtasks /Change /TN $t /DISABLE 2>&1 | Out-Null
        Say "   stopped + disabled task $t"
    }
    # The bridge is a detached node child; the gateway's exit does not take it
    # with it, and a live bridge would keep rewriting session.json underneath
    # the bundle.
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
        Where-Object { $_.CommandLine -match 'plugins.platforms.zalo.bridge' } |
        ForEach-Object {
            Say "   killing bridge pid $($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    Start-Sleep -Seconds 3
} else {
    Say "== LIVE snapshot (services untouched) -- rehearsal mode =="
}

# ------------------------------------------------------- 2. history, safely
$venvPy = Join-Path $AgentDir 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { $venvPy = 'python' }
$srcDb = Join-Path $ProfileHome 'state.db'
$dstDb = Join-Path $profOut 'state.db'
Say "== state.db -> consistent snapshot via sqlite3 backup API =="
$tmpPy = Join-Path $stage '_backup.py'
@(
    'import sqlite3, sys'
    'src, dst = sys.argv[1], sys.argv[2]'
    's = sqlite3.connect("file:" + src.replace("\\", "/") + "?mode=ro", uri=True)'
    'd = sqlite3.connect(dst)'
    'with d:'
    '    s.backup(d)'
    'n = list(d.execute("SELECT (SELECT COUNT(*) FROM sessions),(SELECT COUNT(*) FROM messages)"))[0]'
    'd.execute("PRAGMA journal_mode=DELETE")'
    'd.close(); s.close()'
    'print("   %d sessions, %d messages" % n)'
) | Set-Content -Path $tmpPy -Encoding utf8
& $venvPy $tmpPy $srcDb $dstDb
if ($LASTEXITCODE -ne 0) { Die "state.db snapshot failed" }
Remove-Item $tmpPy -Force

# --------------------------------------------------------- 3. the rest of it
$excludeDirs = @(
    'node_modules', '__pycache__', 'cache', 'audio_cache', 'image_cache',
    'sandboxes', 'terminal-sessions', 'logs', '.curator_backups',
    'backup-20260904'
)
$excludeFiles = @(
    'state.db', 'state.db-wal', 'state.db-shm',
    'models_dev_cache.json', 'models_dev_cache.etag',
    'provider_models_cache.json', 'ollama_cloud_models_cache.json',
    '.skills_prompt_snapshot.json', '.update_check', '.hermes_history',
    'gateway.pid', 'gateway.lock', 'gateway_state.json', 'gateway-starts.log',
    'auth.lock', '.mcp-discovery.lock', '.health-state',
    # A stale lock carried to a new host makes the FTS rebuild believe another
    # process already holds it, and the search index silently never rebuilds.
    'state.db.fts_rebuild.lock'
)

Say "== copying profile state =="
Get-ChildItem -LiteralPath $ProfileHome -Force | ForEach-Object {
    $name = $_.Name
    if ($excludeDirs -contains $name) { return }
    if ($name -like '*.bak*') { return }
    if ($_.PSIsContainer) {
        if ($NoAttachments -and $name -eq 'zalo') {
            $z = Join-Path $profOut 'zalo'
            New-Item -ItemType Directory -Force -Path $z | Out-Null
            Get-ChildItem -LiteralPath $_.FullName -Force |
                Where-Object { $_.Name -notin @('attachments', 'rotated', '.health-state') } |
                ForEach-Object { Copy-Item $_.FullName $z -Recurse -Force }
            Say "   zalo/ (without attachments)"
            return
        }
        Copy-Item $_.FullName $profOut -Recurse -Force
        Say "   $name/"
    } else {
        if ($excludeFiles -contains $name) { return }
        Copy-Item $_.FullName $profOut -Force
    }
}
# These describe the old host and must not be restored.
foreach ($junk in 'zalo\.health-state', 'zalo\rotated') {
    $p = Join-Path $profOut $junk
    if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}

# ------------------------------------------------- 4. the fidelity manifest
# Measured from THIS host, not hardcoded: the Linux installer reads it, so the
# pin cannot drift away from what production actually ran.
Say "== manifest: what the new host must reproduce =="
function GitAt($d, $a) { & git -C $d @a 2>$null }
$odooDist = Get-ChildItem "$env:APPDATA\uv\tools\odoo-mcp\Lib\site-packages" `
                -Filter 'odoo_mcp-*.dist-info' -ErrorAction SilentlyContinue |
            Select-Object -First 1
$odooMcp = if ($odooDist) { $odooDist.Name -replace '^odoo_mcp-|\.dist-info$', '' } else { '' }
$coreMods = (@(GitAt $AgentDir @('status', '--short', '--untracked-files=no')) -join "`n")

$manifest = [ordered]@{
    created_at          = (Get-Date).ToString('o')
    final               = [bool]$Final
    source_host         = $env:COMPUTERNAME
    profile             = 'zalo-bot'
    agent_upstream      = (GitAt $AgentDir @('config', '--get', 'remote.origin.url'))
    agent_commit        = (GitAt $AgentDir @('rev-parse', 'HEAD'))
    agent_core_modified = $coreMods
    fork_remote         = (GitAt $ForkDir @('config', '--get', 'remote.origin.url'))
    fork_branch         = (GitAt $ForkDir @('rev-parse', '--abbrev-ref', 'HEAD'))
    fork_commit         = (GitAt $ForkDir @('rev-parse', 'HEAD'))
    python              = "$(& $venvPy --version 2>&1)"
    node                = "$(& node --version 2>$null)"
    uv                  = "$(& uv --version 2>$null)"
    odoo_mcp            = "$odooMcp"
    patches_required    = @(
        'fix-mcp-stdio-liveness.py       -> tools/mcp_tool.py',
        'fix-compaction-status-leak.py   -> gateway/run.py',
        'fix-execute-method-field-acl.py -> odoo_mcp/tools_write.py'
    )
    attachments_included = (-not $NoAttachments)
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $stage 'MANIFEST.json') -Encoding utf8
Get-Content (Join-Path $stage 'MANIFEST.json') | Write-Host

if ($coreMods -notmatch 'gateway/run\.py' -or $coreMods -notmatch 'tools/mcp_tool\.py') {
    Write-Host "WARNING: the install's modified-core list is not the two expected patches." -ForegroundColor Yellow
    Write-Host "         Check it before trusting the Linux side to reproduce this runtime." -ForegroundColor Yellow
}

# ------------------------------------------------------------- 5. archive it
$tar = Join-Path $OutDir "zalo-bot-bundle-$stamp.tar.gz"
Say "== archiving -> $tar =="
& tar -czf $tar -C $stage profile MANIFEST.json
if ($LASTEXITCODE -ne 0) { Die "tar failed" }

$hash = (Get-FileHash $tar -Algorithm SHA256).Hash.ToLower()
# LF, written directly: Set-Content emits CRLF on PowerShell 5.1, and GNU
# sha256sum then treats the trailing \r as part of the filename and reports
# "No such file or directory / FAILED open or read". Measured: the Linux
# installer aborted with "checksum mismatch -- re-copy the bundle" on a bundle
# that was byte-perfect, which sends you hunting a transfer problem that does
# not exist.
[IO.File]::WriteAllText("$tar.sha256", "$hash  $(Split-Path $tar -Leaf)`n",
                        (New-Object System.Text.ASCIIEncoding))
Remove-Item $stage -Recurse -Force

$mb = [math]::Round((Get-Item $tar).Length / 1MB, 1)
Say ""
Say "bundle : $tar  (${mb} MB)"
Say "sha256 : $hash"
Say ""
Say "Copy both files to the Linux host, e.g."
Say "  scp `"$tar`" `"$tar.sha256`" hermes@NEW-HOST:~/"
if ($Final) {
    Say ""
    Say "The bot is now DOWN and both tasks are DISABLED."
    Say "To roll back to this host:"
    Say "  schtasks /Change /TN HermesZaloGateway /ENABLE"
    Say "  schtasks /Change /TN HermesZaloHealth  /ENABLE"
    Say "  schtasks /Run    /TN HermesZaloGateway"
}
