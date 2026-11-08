<#
.SYNOPSIS
    把树莓派运行所需的文件整理成一个可直接拷贝的文件夹，可选直接上传。

.DESCRIPTION
    与"手工复制 + 一长串排除规则"相比，这里用**白名单**：
    只有下面 $Items 里列出的路径会进入输出目录。数据库、JWT 密钥、旧桌面库、
    训练权重（.pt）、数据集、node_modules 从机制上就进不来。

    输出是**普通文件夹**，不是压缩包：用 FileZilla / WinSCP / 资源管理器
    把整个 InsectPestDetection 文件夹拖到树莓派即可，盒子端不需要解压。

    整理完成后还会做一次安全闸门检查：一旦发现 app.db* / .jwt_secret /
    UserDatabase.db / *.pt，直接中止并列出文件，而不是"打了再说"。

.PARAMETER AgentOnly
    只当采集设备：不包含 vue-frontend/dist、test_media 与知识库配图。
    体积从约 48 MB 降到约 38 MB。盒子不提供网页时用这个。

.PARAMETER Upload
    生成后用 scp -r 递归上传到 -Pi 指定的主机（需要 Windows 的 OpenSSH 客户端）。

.PARAMETER SkipBuild
    缺少 vue-frontend/dist 时不自动构建，直接报错。

.PARAMETER OutDir
    输出目录（里面会生成 InsectPestDetection 子目录）。
    默认 ..\insect-pi-folder\InsectPestDetection。

.PARAMETER Force
    目标 InsectPestDetection 已存在且不像是本脚本生成的包时，仍然覆盖删除。

.PARAMETER Folder
    兼容旧命令保留，没有实际作用：现在**默认就是输出文件夹**。

.EXAMPLE
    .\deploy\pack-for-pi.ps1

.EXAMPLE
    .\deploy\pack-for-pi.ps1 -AgentOnly -Pi pi@192.168.1.50 -Upload

.EXAMPLE
    .\deploy\pack-for-pi.ps1 -OutDir D:\tmp
    # 得到 D:\tmp\InsectPestDetection\，整个文件夹拷到树莓派即可，无需解压
#>
[CmdletBinding()]
param(
    [string]$Pi = "",
    [string]$RemoteDir = "/tmp",
    [switch]$AgentOnly,
    [switch]$Upload,
    [switch]$SkipBuild,
    [switch]$KeepStage,
    [switch]$Folder,
    [string]$OutDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# PowerShell 5.1 在输出被重定向时默认用控制台代码页，中文会变成乱码。
# 显式设成 UTF-8，Git Bash / VSCode 终端 / 管道里都能正常显示。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch { }

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

$RemoteDir = $RemoteDir.TrimEnd("/")
if (-not $RemoteDir) { $RemoteDir = "/tmp" }

$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root "backend\app\main.py"))) {
    throw "找不到 backend\app\main.py。请把本脚本放在项目的 deploy\ 目录下运行。"
}

# ---------------------------------------------------------------- 前置检查
Write-Step "检查打包所需文件"

$Model = Join-Path $Root "weights\edge\best-yolo11s-640.onnx"
if (-not (Test-Path $Model)) {
    throw ("缺少 ONNX 模型：weights\edge\best-yolo11s-640.onnx`n" +
        "请在开发机上先导出：python run_export_model.py --model weights/best-yolo11s.pt --format onnx --imgsz 640")
}

$DistIndex = Join-Path $Root "vue-frontend\dist\index.html"
if (-not $AgentOnly -and -not (Test-Path $DistIndex)) {
    if ($SkipBuild) {
        throw "缺少 vue-frontend\dist，且指定了 -SkipBuild。先在 vue-frontend 里执行 npm run build。"
    }
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw "缺少 vue-frontend\dist，且找不到 npm。请在开发机上构建：cd vue-frontend; npm install; npm run build"
    }
    Write-Step "vue-frontend\dist 不存在，先构建前端"
    Push-Location (Join-Path $Root "vue-frontend")
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build 失败（exit $LASTEXITCODE）" }
    }
    finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------- 白名单
$Items = @(
    "backend\app",
    "backend\tests",
    "backend\migrations",
    "backend\requirements-edge.txt",
    "backend\constraints.txt",
    "backend\pytest.ini",
    "weights\edge",
    "deploy",
    "tools",
    "项目文档.md"
)
if (-not $AgentOnly) {
    $Items += "vue-frontend\dist"
    $Items += "test_media"
    $Items += "backend\data\species_samples"
}

$Stage = Join-Path $env:TEMP ("insect-pi-stage-" + [guid]::NewGuid().ToString("N"))
$StageRel = Join-Path $Stage "InsectPestDetection"
New-Item -ItemType Directory -Path $StageRel -Force | Out-Null

try {
    Write-Step "按白名单收集文件"
    foreach ($rel in $Items) {
        $src = Join-Path $Root $rel
        if (-not (Test-Path $src)) {
            throw "白名单里的路径不存在：$rel"
        }
        $dst = Join-Path $StageRel $rel
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

    # ------------------------------------------------------------ 去掉缓存
    Get-ChildItem -LiteralPath $StageRel -Recurse -Force -Directory |
        Where-Object { $_.Name -eq "__pycache__" -or $_.Name -eq ".pytest_cache" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    Get-ChildItem -LiteralPath $StageRel -Recurse -Force -File |
        Where-Object { $_.Extension -eq ".pyc" -or $_.Extension -eq ".pyo" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

    # 目标树会被 Windows 工具直接拷到树莓派：如果 .sh 被任何环节转成了 CRLF，
    # bash 会在第一行 `set -euo pipefail` 就报错。统一成 LF。
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    Get-ChildItem -LiteralPath $StageRel -Recurse -Force -File -Filter "*.sh" |
        ForEach-Object {
            $text = [System.IO.File]::ReadAllText($_.FullName)
            if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) {
                $text = $text.Substring(1)
            }
            if ($text.Contains("`r`n")) {
                [System.IO.File]::WriteAllText($_.FullName, $text.Replace("`r`n", "`n"), $Utf8NoBom)
                Write-Host ("    已转成 LF：{0}" -f $_.FullName.Substring($StageRel.Length + 1))
            }
        }

    # ------------------------------------------------------------ 安全闸门
    $Forbidden = Get-ChildItem -LiteralPath $StageRel -Recurse -Force -File |
        Where-Object {
            $_.Name -eq ".jwt_secret" -or
            $_.Name -eq ".env" -or
            $_.Name -eq "UserDatabase.db" -or
            $_.Name -like "app.db*" -or
            $_.Extension -eq ".pt" -or
            $_.Extension -eq ".pth"
        }
    if ($Forbidden) {
        $names = ($Forbidden | ForEach-Object { $_.FullName.Substring($StageRel.Length + 1) }) -join "`n    "
        throw "输出目录里出现了不该带的文件（已中止）：`n    $names"
    }

    $StageOnnx = Get-ChildItem -LiteralPath (Join-Path $StageRel "weights\edge") -Filter "*.onnx" -File -ErrorAction SilentlyContinue
    if (-not $StageOnnx) {
        throw "weights\edge 下没有 .onnx，树莓派无法推理"
    }

    # ------------------------------------------------------------ 体积
    $Files = Get-ChildItem -LiteralPath $StageRel -Recurse -Force -File
    $TotalMb = [math]::Round((($Files | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
    Write-Step ("待输出 {0} 个文件，共 {1} MB" -f $Files.Count, $TotalMb)
    Get-ChildItem -LiteralPath $StageRel -Force | ForEach-Object {
        $sum = (Get-ChildItem -LiteralPath $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
        if ($null -eq $sum) { $sum = 0 }
        Write-Host ("    {0,-28} {1,7:N1} MB" -f $_.Name, ($sum / 1MB))
    }

    # ------------------------------------------------------------ 输出文件夹
    Write-Step "输出为可直接拷贝的文件夹（树莓派端不需要解压）"
    if (-not $OutDir) {
        $OutDir = Join-Path (Split-Path -Parent $Root) "insect-pi-folder"
    }
    elseif (-not [System.IO.Path]::IsPathRooted($OutDir)) {
        $OutDir = Join-Path $Root $OutDir
    }
    $OutDir = [System.IO.Path]::GetFullPath($OutDir)
    $OutFolder = Join-Path $OutDir "InsectPestDetection"

    if (Test-Path $OutFolder) {
        # 只自动覆盖"上一轮自己生成的包"，别误删用户自己的同名目录。
        $LooksLikeOurs = (Test-Path (Join-Path $OutFolder "backend\app\main.py")) -and
                         (Test-Path (Join-Path $OutFolder "deploy\install-pi.sh"))
        if (-not $LooksLikeOurs -and -not $Force) {
            throw ("目标文件夹已存在，且看起来不是本脚本生成的包：`n    $OutFolder`n" +
                "换个 -OutDir，或确认后加 -Force（会整个删除它）。")
        }
        Remove-Item -LiteralPath $OutFolder -Recurse -Force
    }
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    Copy-Item -LiteralPath $StageRel -Destination $OutDir -Recurse -Force

    $FolderMb = [math]::Round(((Get-ChildItem -LiteralPath $OutFolder -Recurse -Force -File |
        Measure-Object -Property Length -Sum).Sum / 1MB), 1)
    Write-Host ("    已生成：{0}（{1} MB）" -f $OutFolder, $FolderMb)

    # ------------------------------------------------------------ 上传
    if ($Upload) {
        if (-not $Pi) { throw "-Upload 需要同时给 -Pi user@host" }
        if (-not (Get-Command scp -ErrorAction SilentlyContinue)) {
            throw "找不到 scp。Windows 设置 → 应用 → 可选功能里安装 OpenSSH 客户端。"
        }
        Write-Step ("递归上传到 {0}:{1}/" -f $Pi, $RemoteDir)
        & scp -r $OutFolder ("{0}:{1}/" -f $Pi, $RemoteDir)
        if ($LASTEXITCODE -ne 0) { throw "scp -r 失败（exit $LASTEXITCODE）" }
    }

    # ------------------------------------------------------------ 下一步
    $InstallFlags = "--center http://<电脑IP>:8000 --service"
    if ($AgentOnly) { $InstallFlags += " --agent-only" }

    Write-Host ""
    Write-Host "下一步（树莓派上执行）：" -ForegroundColor Green
    Write-Host '  sudo mkdir -p /opt/insect && sudo chown "$USER:$USER" /opt/insect'
    if ($Upload) {
        Write-Host ("  # scp -r 已放到 {0}/InsectPestDetection；挪过去：" -f $RemoteDir)
        Write-Host ("  mv {0}/InsectPestDetection /opt/insect/" -f $RemoteDir)
    }
    else {
        Write-Host "  # 用 FileZilla / WinSCP 把整个 InsectPestDetection 文件夹"
        Write-Host "  # 直接拖到树莓派的 /opt/insect/ 下（无需解压）"
    }
    Write-Host "  cd /opt/insect/InsectPestDetection"
    Write-Host ("  bash deploy/install-pi.sh {0}" -f $InstallFlags)
    Write-Host ""
    if ($AgentOnly) {
        Write-Host "  已加 --agent-only；CSI 排线摄像头再加 --csi。"
    }
    else {
        Write-Host "  只当采集设备（不提供网页）时加 --agent-only；CSI 排线摄像头加 --csi。"
    }
    Write-Host "  绑定在**中心**网页完成：我的设备 → 绑定设备 → 输入安装脚本打印的 6 位码。"
}
finally {
    if ($KeepStage) {
        Write-Host ""
        Write-Host "暂存目录保留在：$Stage"
    }
    elseif (Test-Path $Stage) {
        Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}

