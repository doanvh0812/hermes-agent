$ErrorActionPreference = 'Stop'

$HermesRoot  = 'C:\Users\USER\AppData\Local\hermes'
$ProfileHome = 'C:\Users\USER\AppData\Local\hermes\profiles\zalo-bot'
$env:HERMES_HOME = $ProfileHome
$env:PATH = "$HermesRoot\bin;$env:PATH"

$logDir = Join-Path $ProfileHome 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$out = Join-Path $logDir 'admin.log'
$err = Join-Path $logDir 'admin.err.log'
foreach ($f in @($out, $err)) {
    if (Test-Path $f) { Move-Item $f "$f.prev" -Force -ErrorAction SilentlyContinue }
}

$proc = Start-Process `
    -FilePath (Join-Path $HermesRoot 'hermes-agent\venv\Scripts\python.exe') `
    -ArgumentList 'C:\Users\USER\AppData\Local\hermes\hermes-agent\plugins\platforms\zalo\admin\server.py' `
    -WorkingDirectory 'C:\Users\USER\AppData\Local\hermes\hermes-agent\plugins\platforms\zalo\admin' `
    -RedirectStandardOutput $out -RedirectStandardError $err `
    -NoNewWindow -PassThru

# LOAD-BEARING, cung loi da sua trong run-gateway.ps1: Start-Process -PassThru
# tra ve object co ExitCode = $null neu khong cham .Handle truoc, nen
# `exit $proc.ExitCode` ben duoi luon thoat 0 bat ke panel chet the nao.
$null = $proc.Handle

# LUU Y khi khoi dong lai: khac gateway, server.py KHONG co co --replace va no
# giu cong 8648. `schtasks /End` chi giet wrapper, tien trinh python tach roi
# van giu cong, nen `schtasks /Run` sau do khong bind duoc va chet ngay. Muon
# khoi dong lai panel thi phai diet tien trinh python truoc roi moi /Run.
try { $proc.WaitForExit(); exit $proc.ExitCode }
finally { if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } }
