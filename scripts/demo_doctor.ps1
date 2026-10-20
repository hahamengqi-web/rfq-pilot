[CmdletBinding()]
param()

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$checks = [System.Collections.Generic.List[object]]::new()

function Add-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    $checks.Add([pscustomobject]@{ Name=$Name; Status=$(if($Ok){"通过"}else{"失败"}); Detail=$Detail })
}
function Test-Port([int]$Port) {
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $ok = $client.ConnectAsync("127.0.0.1", $Port).Wait(800) -and $client.Connected
        $client.Dispose(); return $ok
    } catch { return $false }
}
function Test-Url([string]$Url) {
    try { return (Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 4).StatusCode -lt 500 }
    catch { return $false }
}

Add-Check "Python 环境" (Test-Path ".venv\Scripts\python.exe") "项目虚拟环境"
Add-Check "Docker" ([bool](Get-Command docker -ErrorAction SilentlyContinue)) "Docker 命令"
foreach ($item in @(@("MySQL",3306),@("Redis",6379),@("Milvus",19530),@("Elasticsearch",9201))) {
    Add-Check $item[0] (Test-Port $item[1]) "127.0.0.1:$($item[1])"
}
Add-Check "客户智能客服" (Test-Url "http://127.0.0.1:8766/health") "http://127.0.0.1:8766/"
Add-Check "内部工作台" (Test-Url "http://127.0.0.1:8767/healthz") "http://127.0.0.1:8767/"
Add-Check "RAG 就绪" (Test-Url "http://127.0.0.1:8767/readyz") "Milvus 与 Elasticsearch"
Add-Check "询盘工作流 API" (Test-Url "http://127.0.0.1:8767/api/inquiries") "导入、审批与报价入口"
try {
    $uiConfig = Invoke-RestMethod -Uri "http://127.0.0.1:8767/api/ui/config" -TimeoutSec 4
    $demoModeReady = [bool]$uiConfig.demo_mode
    $demoModeDetail = if ($uiConfig.demo_fixed_data) { "固定演示数据" } else { "真实接口优先，失败降级" }
} catch {
    $demoModeReady = $false
    $demoModeDetail = "配置读取失败"
}
Add-Check "询盘演示模式" $demoModeReady $demoModeDetail
if ($demoModeReady -and $uiConfig.demo_fixed_data) {
    try {
        $emailRuntime = Invoke-RestMethod -Uri "http://127.0.0.1:8767/api/email/runtime" -TimeoutSec 4
        $mailIsolated = [bool]$emailRuntime.inquiry_smtp_enabled
        if ($uiConfig.demo_live_mailbox) {
            $mailIsolated = $mailIsolated -and [bool]$emailRuntime.managed_ingestion_enabled -and
                -not [bool]$emailRuntime.managed_ingestion_paused_for_demo
        } else {
            $mailIsolated = $mailIsolated -and [bool]$emailRuntime.managed_ingestion_paused_for_demo -and
                -not [bool]$emailRuntime.managed_ingestion_enabled
        }
    } catch { $mailIsolated = $false }
    Add-Check "固定演示收发策略" $mailIsolated $(if($uiConfig.demo_live_mailbox){"真实邮件审批后发件；固定样例仅本地模拟"}else{"固定样例仅本地模拟"})
}
Add-Check "演示询盘邮件" (Test-Path "examples\demo\northstar-inquiry.eml") "固定导入样例"
$demoMailbox = Join-Path $projectRoot "runtime\demo-mailbox"
try {
    New-Item -ItemType Directory -Force -Path $demoMailbox | Out-Null
    $probe = Join-Path $demoMailbox ".write-check"
    Set-Content -LiteralPath $probe -Value "ok" -Encoding ascii
    Remove-Item -LiteralPath $probe -Force
    $mailboxWritable = $true
} catch { $mailboxWritable = $false }
Add-Check "模拟发件箱" $mailboxWritable "runtime\demo-mailbox"

$envText = if (Test-Path ".env") { Get-Content ".env" -Raw } else { "" }
$hasMainKey = $envText -match "(?m)^NANOCLAW_API_KEY=\S+"
$hasReportKey = $envText -match "(?m)^DUE_DILIGENCE_REPORT_API_KEY=\S+"
Add-Check "主模型密钥" $hasMainKey "只检查是否填写，不显示内容"
Add-Check "报告模型密钥" ($hasReportKey -or $hasMainKey) "DeepSeek Key 或主模型降级"

$checks | Format-Table -AutoSize
$failed = @($checks | Where-Object Status -eq "失败")
if ($failed.Count) {
    Write-Host "`n演示检查未通过：$($failed.Count) 项。" -ForegroundColor Red
    exit 1
}
Write-Host "`n演示检查通过，可以开始演示。" -ForegroundColor Green
