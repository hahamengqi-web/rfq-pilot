$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot "workspace\runtime\demo-main.pid"

function Stop-ProcessTree([int]$TargetId) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$TargetId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) { Stop-ProcessTree ([int]$child.ProcessId) }
    Stop-Process -Id $TargetId -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $pidFile) {
    $processId = [int](Get-Content -LiteralPath $pidFile -Raw)
    Stop-ProcessTree $processId
    Remove-Item -LiteralPath $pidFile -Force
}
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(8766, 8767) })
foreach ($ownerId in @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId" -ErrorAction SilentlyContinue
    if ($process -and $process.CommandLine -match '(?i)python(?:\.exe)?"?\s+main\.py') {
        Stop-ProcessTree ([int]$process.ProcessId)
    }
}
Write-Host "RFQPilot 演示应用已停止。数据库容器继续保留，避免下次启动重新初始化。"
