<#
.SYNOPSIS
    把代码改动增量同步到已在运行的树莓派，并重启后端。

.DESCRIPTION
    只传运行时会变的部分：backend/app、backend/migrations、tools、
    backend/requirements-edge.txt，以及（默认）vue-frontend/dist。
    远端用 apply-update.sh 做"新目录改名"替换，不会留下已删除的旧模块。

    传输方式与 pack-for-pi.ps1 一致：**直接 scp -r 一个目录**，
    不在开发机打包、也不在树莓派解压。

    第一次部署、或改了 deploy/ 下的服务单元/脚本，请用 pack-for-pi.ps1
    重新打完整包，这个脚本不替换 deploy/。

.PARAMETER WithDeps
    requirements-edge.txt 变了时加上它，远端会执行 pip install。

.PARAMETER NoRestart
    只同步文件，不重启服务（下一次重启时生效）。

.PARAMETER DryRun
    只在本机生成增量目录并打印远端命令，不 scp / ssh。

.EXAMPLE
    .\deploy\update-pi.ps1 -Pi pi@192.168.1.50

.EXAMPLE
    .\deploy\update-pi.ps1 -Pi pi@192.168.1.50 -WithDeps
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Pi,
    [string]$RemoteDir = "/opt/insect/InsectPestDetection",
    [switch]$AgentOnly,
    [switch]$WithDeps,
    [switch]$NoRestart,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# 与 pack-for-pi.ps1 同理：重定向时避免中文乱码。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch { }

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root "backend\app\main.py"))) {
    throw "找不到 backend\app\main.py。请把本脚本放在项目的 deploy\ 目录下运行。"
}
if (-not $DryRun) {
    if (-not (Get-Command scp -ErrorAction SilentlyContinue) -or
        -not (Get-Command ssh -ErrorAction SilentlyContinue)) {
        throw "找不到 scp/ssh。Windows 设置 → 应用 → 可选功能里安装 OpenSSH 客户端。"
    }
}

$DistIndex = Join-Path $Root "vue-frontend\dist\index.html"
if (-not $AgentOnly -and -not (Test-Path $DistIndex)) {
    throw "缺少 vue-frontend\dist。先在 vue-frontend 里 npm run build；只当采集设备则加 -AgentOnly。"
}

# ---------------------------------------------------------------- 增量包内容
$Items = @(
    "backend\app",
    "backend\migrations",
    "backend\requirements-edge.txt",
    "tools"
)
if (-not $AgentOnly) {
    $Items += "vue-frontend\dist"
}

$Stage = Join-Path $env:TEMP ("insect-update-stage-" + [guid]::NewGuid().ToString("N"))
$PayloadDir = Join-Path $Stage "payload"
New-Item -ItemType Directory -Path $PayloadDir -Force | Out-Null

try {
    Write-Step "收集增量文件"
    foreach ($rel in $Items) {
        $src = Join-Path $Root $rel
        if (-not (Test-Path $src)) {
            throw "增量清单里的路径不存在：$rel"
        }
        $dst = Join-Path $PayloadDir $rel
        $parent = Split-Path -Parent $dst
        if (-not (Test-Path $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        if (Test-Path $src -PathType Container) {
            New-Item -ItemType Directory -Path $dst -Force | Out-Null
            Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
        }
        else {
            Copy-Item -LiteralPath $src -Destination $dst -Force
        }
        Write-Host ("    + {0}" -f $rel)
    }

    Get-ChildItem -LiteralPath $PayloadDir -Recurse -Force -Directory |
        Where-Object { $_.Name -eq "__pycache__" -or $_.Name -eq ".pytest_cache" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    Get-ChildItem -LiteralPath $PayloadDir -Recurse -Force -File |
        Where-Object { $_.Extension -eq ".pyc" -or $_.Extension -eq ".pyo" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

    $PayloadMb = [math]::Round(((Get-ChildItem -LiteralPath $PayloadDir -Recurse -Force -File |
        Measure-Object -Property Length -Sum).Sum / 1MB), 1)
    Write-Host ("    增量目录已就绪（{0} MB）" -f $PayloadMb)

    $remoteCmd = "bash /tmp/insect-apply-update.sh '{0}'" -f $RemoteDir
    if ($WithDeps) { $remoteCmd += " --with-deps" }
    if ($NoRestart) { $remoteCmd += " --no-restart" }

    if ($DryRun) {
        $Copy = Join-Path $env:TEMP "insect-update-payload"
        if (Test-Path $Copy) { Remove-Item -LiteralPath $Copy -Recurse -Force }
        Copy-Item -LiteralPath $PayloadDir -Destination $Copy -Recurse -Force
        Write-Host ""
        Write-Host "DryRun：未上传。增量目录在：$Copy" -ForegroundColor Yellow
        Write-Host "远端会执行："
        Write-Host "  ssh ${Pi} `"rm -rf /tmp/insect-update-payload`""
        Write-Host "  scp -r `"$Copy`" ${Pi}:/tmp/insect-update-payload"
        Write-Host "  scp `"$Root\deploy\apply-update.sh`" ${Pi}:/tmp/insect-apply-update.sh"
        $sshFlag = if ($NoRestart) { "" } else { " -t" }
        Write-Host ("  ssh{0} {1} `"{2}`"" -f $sshFlag, $Pi, $remoteCmd)
        return
    }

    Write-Step "上传增量目录"
    # 先清掉上一次的目录：scp -r 只会合并，删掉的文件会残留。
    & ssh $Pi "rm -rf /tmp/insect-update-payload"
    if ($LASTEXITCODE -ne 0) { throw "清理远端旧增量目录失败（exit $LASTEXITCODE）" }
    & scp -r $PayloadDir ("{0}:/tmp/insect-update-payload" -f $Pi)
    if ($LASTEXITCODE -ne 0) { throw "scp -r 增量目录失败（exit $LASTEXITCODE）" }

    & scp (Join-Path $Root "deploy\apply-update.sh") ("{0}:/tmp/insect-apply-update.sh" -f $Pi)
    if ($LASTEXITCODE -ne 0) { throw "scp apply-update.sh 失败（exit $LASTEXITCODE）" }

    Write-Step "远端应用并重启"
    if ($NoRestart) {
        & ssh $Pi $remoteCmd
    }
    else {
        # -t 让远端 sudo 能提示密码（若没有免密 sudo）。
        & ssh -t $Pi $remoteCmd
    }
    $code = $LASTEXITCODE
    if ($code -eq 3) {
        Write-Warning "文件已更新，但重启失败。请手动执行："
        Write-Host "  ssh -t $Pi `"sudo systemctl restart insect-backend`""
        exit $code
    }
    if ($code -ne 0) {
        throw "远端更新失败（exit $code）。查看日志：ssh $Pi `"sudo journalctl -u insect-backend -n 80 --no-pager`""
    }

    Write-Host ""
    Write-Host "完成。改了 config.yaml 或 deploy/ 时，请用 pack-for-pi.ps1 重新部署。" -ForegroundColor Green
}
finally {
    if (Test-Path $Stage) {
        Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}
