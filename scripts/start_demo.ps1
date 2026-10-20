[CmdletBinding()]
param(
    [switch]$UseRealExternalServices,
    [switch]$SkipDataServices
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Wait-TcpPort([string]$HostName, [int]$Port, [int]$TimeoutSeconds = 90) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $client = [System.Net.Sockets.TcpClient]::new()
            $pending = $client.ConnectAsync($HostName, $Port)
            if ($pending.Wait(700) -and $client.Connected) {
                $client.Dispose()
                return $true
            }
            $client.Dispose()
        } catch {}
        Start-Sleep -Milliseconds 700
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Wait-Http([string]$Url, [int]$TimeoutSeconds = 120) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Stop-ProcessTree([int]$TargetId) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$TargetId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) { Stop-ProcessTree ([int]$child.ProcessId) }
    Stop-Process -Id $TargetId -Force -ErrorAction SilentlyContinue
}

function Stop-NanoClawListeners {
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in @(8766, 8767) })
    foreach ($ownerId in @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId" -ErrorAction SilentlyContinue
        if (-not $process -or $process.CommandLine -notmatch '(?i)python(?:\.exe)?"?\s+main\.py') {
            throw "端口 8766/8767 被其他程序占用，请关闭占用程序后重试。"
        }
        $rootId = [int]$process.ProcessId
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" -ErrorAction SilentlyContinue
        if ($parent -and $parent.CommandLine -match '(?i)python(?:\.exe)?"?\s+main\.py') {
            $rootId = [int]$parent.ProcessId
        }
        Stop-ProcessTree $rootId
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 Docker。请先安装并打开 Docker Desktop。"
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop 尚未就绪，请打开后重新运行。" }

Write-Host "[1/5] 启动 MySQL 和 Redis..."
docker compose up -d mysql redis
if ($LASTEXITCODE -ne 0) { throw "MySQL 或 Redis 启动失败。" }

$dataServicesStarted = $false
if (-not $SkipDataServices) {
    $dataEnv = Join-Path $projectRoot "deploy\docker\.env"
    $dataCompose = Join-Path $projectRoot "deploy\docker\compose.yaml"
    if (Test-Path -LiteralPath $dataEnv) {
        Write-Host "[2/5] 启动 Milvus 和 Elasticsearch..."
        docker compose --env-file $dataEnv -f $dataCompose --profile vector-milvus --profile keyword-elasticsearch up -d
        if ($LASTEXITCODE -ne 0) { throw "Milvus 或 Elasticsearch 启动失败。" }
        $dataServicesStarted = $true
    } else {
        Write-Warning "缺少 deploy\docker\.env，未自动启动 Milvus 和 Elasticsearch。"
    }
}

foreach ($service in @(@("MySQL",3306),@("Redis",6379))) {
    if (-not (Wait-TcpPort "127.0.0.1" $service[1] 90)) { throw "$($service[0]) 未在规定时间内就绪。" }
}
if ($dataServicesStarted) {
    foreach ($service in @(@("Milvus",19530),@("Elasticsearch",9201))) {
        if (-not (Wait-TcpPort "127.0.0.1" $service[1] 120)) { throw "$($service[0]) 未在规定时间内就绪。" }
    }
}

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "缺少 .venv。请先执行 uv sync --frozen。" }
if (-not (Test-Path -LiteralPath "examples\demo\northstar-inquiry.eml")) {
    throw "缺少固定演示邮件 examples\demo\northstar-inquiry.eml。"
}
& $python -m scripts.migrate_email_accounts_mysql --apply
if ($LASTEXITCODE -ne 0) { throw "邮箱账户 MySQL 初始化失败。" }

$env:DEMO_MODE = "true"
$env:DEMO_PREFER_FIXTURES = if ($UseRealExternalServices) { "false" } else { "true" }
$env:DEMO_LIVE_MAILBOX = "true"
$env:INQUIRY_SMTP_ENABLED = "true"
$env:NANOCLAW_EMAIL_CLARIFICATION_AUTO_REPLY_ENABLED = "true"
$env:NANOCLAW_EMAIL_SMTP_WORKER_ENABLED = "false"
# The web demo does not use chat-platform bots. Keeping these credentials out
# of the demo process avoids unrelated websocket failures and prevents vendor
# SDK connection parameters from being written to the demo log.
$env:NANOCLAW_FEISHU_APP_ID = ""
$env:NANOCLAW_FEISHU_APP_SECRET = ""
$env:NANOCLAW_QQ_APP_ID = ""
$env:NANOCLAW_QQ_APP_SECRET = ""
$env:NANOCLAW_EMAIL_QQ_NOTIFY_ENABLED = "false"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:NANOCLAW_CUSTOMER_HARNESS_TOOL_TIMEOUT_SECONDS = "45"
$env:NANOCLAW_CUSTOMER_HARNESS_TOOL_MAX_ATTEMPTS = "1"
$env:NANOCLAW_CUSTOMER_HARNESS_RESPONSE_CACHE_TTL_SECONDS = "86400"
$pidFile = Join-Path $projectRoot "workspace\runtime\demo-main.pid"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pidFile) | Out-Null
Stop-NanoClawListeners
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
Write-Host "[3/5] 启动 NanoClaw..."
$stdout = Join-Path $projectRoot "workspace\runtime\demo-main.stdout.log"
$stderr = Join-Path $projectRoot "workspace\runtime\demo-main.stderr.log"
$process = Start-Process -FilePath $python -ArgumentList "main.py" -WorkingDirectory $projectRoot `
    -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

Write-Host "[4/5] 等待页面与 RAG 就绪..."
$customerReady = Wait-Http "http://127.0.0.1:8766/health" 120
$workspaceReady = Wait-Http "http://127.0.0.1:8767/healthz" 120
$ragReady = Wait-Http "http://127.0.0.1:8767/readyz" 120
if (-not ($customerReady -and $workspaceReady -and $ragReady)) {
    Write-Host ""
    Write-Host "最近的启动错误：" -ForegroundColor Red
    if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Tail 40 }
    throw "客户页面、内部工作台或 RAG 未就绪。"
}

& (Join-Path $PSScriptRoot "demo_doctor.ps1")
if ($LASTEXITCODE -ne 0) { throw "演示预检未通过，请先处理上方失败项。" }

Write-Host "[5/5] 预热客户智能客服示例问题..."
& $python (Join-Path $PSScriptRoot "demo_customer_warmup.py")
if ($LASTEXITCODE -ne 0) {
    throw "客户智能客服未能生成带引用的示例回答，请检查模型和 RAG 后重试。"
}

Write-Host ""
Write-Host "演示环境已就绪："
Write-Host "  客户智能客服  http://127.0.0.1:8766/"
Write-Host "  企业内部工作台 http://127.0.0.1:8767/"
Write-Host "  询盘工作流    http://127.0.0.1:8767/inquiries"
Write-Host "  外部数据策略  $(if ($UseRealExternalServices) {'真实接口优先，失败转演示数据'} else {'固定演示数据'})"
