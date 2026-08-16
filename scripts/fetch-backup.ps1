# ============================================================
# fetch-backup.ps1 —— 从服务器拉取每日备份包到本机（二次副本）
# ============================================================
# 功能：
#   1. ssh 列出服务器 /var/backups 下的常规备份包（yiban-YYYY-MM-DD.tar.gz，
#      不含手工迁移包如 yiban-signlog-migrate-*）
#   2. 对比本机目录，拉取所有本地缺失的备份包（scp，逐文件）
#   3. 拉取后用 tar -tzf 校验压缩包完整性（解包列表能读出 = gzip 完整）
#   4. 写日志：本机 <Dest>\fetch-backup.log（追加）
#
# 用法（本机 Windows 开发机；需已配置免密 ssh 到服务器）：
#   powershell -ExecutionPolicy Bypass -File scripts\fetch-backup.ps1 `
#       -Server root@120.26.23.83 -Dest D:\backups\yiban
#
# 计划任务（每 7 天一次，示例每周一 09:00，普通权限即可）：
#   schtasks /create /tn "YibanBackupFetch" /f /sc weekly /d MON /st 09:00 /tr `
#     "powershell -ExecutionPolicy Bypass -File D:\code\...\scripts\fetch-backup.ps1 -Server root@120.26.23.83 -Dest D:\backups\yiban"
#
# 说明：2026-08 决策——新服务器约 3 个月后到位，期间备份先落本机
#       （7 天一次），不配置 REMOTE_BACKUP 异机推送；到期后按需改配。
# ============================================================

param(
    [Parameter(Mandatory = $true)]
    [string]$Server,        # 服务器 ssh 目标，如 root@120.26.23.83

    [string]$RemoteDir = "/var/backups",

    [string]$Dest = "$env:USERPROFILE\backups\yiban",

    # 本机保留份数；0 = 保留全部（新服务器到位前建议 0，迁移时要用）
    [int]$Keep = 0
)

$ErrorActionPreference = "Stop"

function Write-Log {
    param([string]$Msg)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Msg
    Write-Host $line
    Add-Content -Path (Join-Path $Dest "fetch-backup.log") -Value $line -Encoding UTF8
}

# ------------------------------------------------------------
# 1) 列出服务器端常规备份包（精确匹配 yiban-YYYY-MM-DD.tar.gz，
#    排除 yiban-signlog-migrate-* 等手工包）
# ------------------------------------------------------------
$pattern = 'yiban-20[0-9][0-9]-[0-9][0-9]-[0-9][0-9].tar.gz'
# 注意：glob 模式不能加引号，否则远端 shell 不展开（会按字面量查找而失败）
$listOut = ssh -o ConnectTimeout=15 $Server "ls -la $RemoteDir/$pattern 2>/dev/null" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "错误：无法连接服务器 $Server 或列出备份目录（ssh 失败，退出码 $LASTEXITCODE）" -ForegroundColor Red
    exit 1
}
if (-not $listOut) {
    Write-Host "服务器上未找到常规备份包（$RemoteDir/$pattern），请检查 backup.sh cron 是否正常" -ForegroundColor Yellow
    exit 1
}

# 解析每行：提取文件名（Linux ls 长格式，文件名在行尾）
$remoteFiles = @()
foreach ($line in $listOut) {
    $m = [regex]::Match($line, 'yiban-\d{4}-\d{2}-\d{2}\.tar\.gz\s*$')
    if ($m.Success) { $remoteFiles += $m.Value }
}
$remoteFiles = $remoteFiles | Sort-Object -Unique
if ($remoteFiles.Count -eq 0) {
    Write-Host "服务器端未解析出备份文件名，中止" -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------
# 2) 对比本机，收集缺失文件
# ------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$localNames = @(Get-ChildItem -Path $Dest -Filter "yiban-*.tar.gz" -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
$missing = @($remoteFiles | Where-Object { $_ -notin $localNames })

$dateToday = Get-Date -Format "yyyy-MM-dd"
Write-Log "=== 备份拉取开始（$dateToday）：服务器 $Server 共 $($remoteFiles.Count) 个备份包，本机缺失 $($missing.Count) 个 ==="

if ($missing.Count -eq 0) {
    Write-Log "本机备份已是最新（最新：$($remoteFiles[-1])），无需拉取"
    exit 0
}

# ------------------------------------------------------------
# 3) 逐文件拉取 + 完整性校验
# ------------------------------------------------------------
$okCount = 0
foreach ($name in $missing) {
    $local = Join-Path $Dest $name
    try {
        scp -p "$Server`:$RemoteDir/$name" $local
        if ($LASTEXITCODE -ne 0) { throw "scp 退出码 $LASTEXITCODE" }
        # gzip 完整性校验：能列出包内文件 = 压缩流完整可读
        $null = tar -tzf $local 2>$null
        if ($LASTEXITCODE -ne 0) { throw "tar 校验失败（包可能损坏）" }
        Write-Log "已拉取并校验：$name"
        $okCount++
    }
    catch {
        Write-Log "拉取失败：$name（$($_.Exception.Message)），跳过，下次运行会重试"
        if (Test-Path $local) { Remove-Item $local -Force -ErrorAction SilentlyContinue }
    }
}

# ------------------------------------------------------------
# 4) 本机保留策略（Keep > 0 时只保留最近 N 份；默认保留全部）
# ------------------------------------------------------------
if ($Keep -gt 0) {
    $all = @(Get-ChildItem -Path $Dest -Filter "yiban-*.tar.gz" | Sort-Object Name -Descending)
    if ($all.Count -gt $Keep) {
        $all | Select-Object -Skip $Keep | ForEach-Object {
            Remove-Item $_.FullName -Force
            Write-Log "本机清理旧备份：$($_.Name)"
        }
    }
}

$localCount = @(Get-ChildItem -Path $Dest -Filter "yiban-*.tar.gz" -ErrorAction SilentlyContinue).Count
Write-Log "=== 拉取结束：成功 $okCount / 缺失 $($missing.Count)（本机现有 $localCount 份）==="
if ($okCount -lt $missing.Count) {
    Write-Host "部分备份拉取失败，请检查网络后手动重跑本脚本" -ForegroundColor Yellow
    exit 1
}
exit 0
