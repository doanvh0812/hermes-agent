$ErrorActionPreference = 'Stop'
$log = 'C:\Users\Administrator\AppData\Local\hermes\profiles\zalo-bot\logs\caddy.log'
foreach ($f in @($log, "$log.err")) {
    if (Test-Path $f) { Move-Item $f "$f.prev" -Force -ErrorAction SilentlyContinue }
}
# Start-Process, khong phai goi truc tiep: PowerShell 5.1 boc stderr cua native
# exe thanh NativeCommandError, va Caddy ghi toan bo log ra stderr.
$proc = Start-Process -FilePath 'caddy' `
    -ArgumentList @('run','--config','C:\Users\Administrator\AppData\Local\hermes\profiles\zalo-bot\ops\Caddyfile') `
    -RedirectStandardOutput $log -RedirectStandardError "$log.err" `
    -NoNewWindow -PassThru
try { $proc.WaitForExit(); exit $proc.ExitCode }
finally { if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } }
