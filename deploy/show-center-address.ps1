#Requires -Version 5.1
<#
.SYNOPSIS
    打印这台电脑（中心）的局域网地址，并生成树莓派上要执行的那条命令。

.DESCRIPTION
    盒子（树莓派）是靠 `agent.center_url` 主动拨回中心的。这个地址填错，
    表现是"网页上盒子永远离线"，而盒子自己看起来一切正常 —— 很难查。

    本脚本做三件事：
      1. 列出本机真实 IPv4，剔掉回环、APIPA(169.254.*)、以及 Hyper-V /
         VirtualBox / VMware / WSL 这类虚拟网卡的地址（它们填给 Pi 必然不通）；
      2. 逐个探测该地址 + 8000 端口是否真的能取到 /api/health；
      3. 直接吐出树莓派上要执行的完整命令，复制即可。

    用法（双击同目录的"显示中心地址.bat"，或在 PowerShell 里）：
      powershell -ExecutionPolicy Bypass -File deploy\show-center-address.ps1
      powershell -ExecutionPolicy Bypass -File deploy\show-center-address.ps1 -Port 8000
#>
[CmdletBinding()]
param(
    [int]$Port = 8000
)

$ErrorActionPreference = 'Continue'

function Write-Head([string]$text) {
    Write-Host ''
    Write-Host ('=' * 58) -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('=' * 58) -ForegroundColor Cyan
}

# 虚拟网卡描述里的关键词：这些地址给 Pi 是一定拨不通的
$VIRTUAL_HINTS = @(
    'Hyper-V', 'VirtualBox', 'VMware', 'Virtual', 'WSL',
    'Loopback', 'Teredo', 'isatap', 'Bluetooth', 'RAS Async', 'WAN Miniport'
)

# ---------------------------------------------------------------- 1. 地址
Write-Head '1. 本机 IPv4 地址'

$defaultIndex = $null
try {
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
        Sort-Object -Property RouteMetric |
        Select-Object -First 1
    if ($route) { $defaultIndex = $route.InterfaceIndex }
} catch { $defaultIndex = $null }

$adapters = @{}
try {
    Get-NetAdapter -ErrorAction Stop | ForEach-Object {
        $adapters[[int]$_.InterfaceIndex] = $_
    }
} catch { }

$candidates = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -and
        $_.IPAddress -notlike '127.*' -and
        $_.IPAddress -notlike '169.254.*' -and
        $_.IPAddress -ne '0.0.0.0'
    } | Sort-Object -Property InterfaceIndex

if (-not $candidates) {
    Write-Host '  没有找到任何局域网 IPv4 地址。请检查网线 / Wi-Fi 是否已连接。' -ForegroundColor Yellow
    exit 1
}

$rows = @()
foreach ($ip in $candidates) {
    $adapter = $adapters[[int]$ip.InterfaceIndex]
    $desc = if ($adapter) { $adapter.InterfaceDescription } else { '' }
    $name = if ($adapter) { $adapter.Name } else { "接口 #$($ip.InterfaceIndex)" }

    $isVirtual = $false
    foreach ($hint in $VIRTUAL_HINTS) {
        if ($desc -like "*$hint*" -or $name -like "*$hint*") { $isVirtual = $true; break }
    }
    $isDefault = ($defaultIndex -ne $null) -and ([int]$ip.InterfaceIndex -eq [int]$defaultIndex)

    $rows += [PSCustomObject]@{
        Address  = $ip.IPAddress
        Name     = $name
        Desc     = $desc
        Virtual  = $isVirtual
        Default  = $isDefault
    }
}

foreach ($r in $rows) {
    $tag = ''
    if ($r.Default) { $tag += ' [默认路由]' }
    if ($r.Virtual) { $tag += ' [虚拟网卡]' }
    $color = if ($r.Default -and -not $r.Virtual) { 'Green' } elseif ($r.Virtual) { 'DarkGray' } else { 'White' }
    Write-Host ("  {0,-16} {1}{2}" -f $r.Address, $r.Name, $tag) -ForegroundColor $color
    if ($r.Desc) { Write-Host ("                   {0}" -f $r.Desc) -ForegroundColor DarkGray }
}

# 推荐：默认路由上的非虚拟地址；退化到第一个非虚拟地址
$recommended = $rows | Where-Object { $_.Default -and -not $_.Virtual } | Select-Object -First 1
if (-not $recommended) { $recommended = $rows | Where-Object { -not $_.Virtual } | Select-Object -First 1 }
if (-not $recommended) { $recommended = $rows | Select-Object -First 1 }

# ---------------------------------------------------------------- 2. 端口与探活
Write-Head "2. 后端是否真的可达（端口 $Port）"

$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    $bound = ($listening | Select-Object -ExpandProperty LocalAddress | Sort-Object -Unique) -join ', '
    Write-Host "  端口 $Port 正在监听，绑定地址：$bound" -ForegroundColor Green
    if ($bound -notmatch '0\.0\.0\.0' -and $bound -notmatch '\[::\]') {
        Write-Host '  注意：没有绑定 0.0.0.0 —— 树莓派从局域网访问不了，'
        Write-Host '        请检查 config.yaml 的 server.host 是否为 0.0.0.0。' -ForegroundColor Yellow
    }
} else {
    Write-Host "  端口 $Port 没有在监听 —— 后端还没启动。" -ForegroundColor Yellow
    Write-Host '  先双击 start_backend.bat（或 start_all.bat），再重新运行本脚本。'
}

$reachable = @()
foreach ($r in $rows) {
    $url = "http://$($r.Address):$Port/api/health"
    $ok = $false
    $note = ''
    try {
        # 走 .NET 而不是 curl：不受系统代理设置影响（本机代理会把 127/局域网请求拦成 502）
        $req = [System.Net.HttpWebRequest]::Create($url)
        $req.Timeout = 3000
        $req.Proxy = $null
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        $ok = ($code -eq 200)
        $note = "HTTP $code"
    } catch {
        $note = '不通'
    }
    $color = if ($ok) { 'Green' } else { 'DarkGray' }
    Write-Host ("  {0,-16} {1}" -f $r.Address, $note) -ForegroundColor $color
    # 虚拟网卡上的 200 是**假绿灯**：后端绑 0.0.0.0，所以从本机哪个地址都通，
    # 但那些虚拟交换机只存在于这台电脑内部 —— 树莓派根本连不到。
    # 不点破的话，人会挑一个"测下来 200"的虚拟地址，盒子照样离线。
    if ($ok -and $r.Virtual) {
        Write-Host '                   本机可达，但树莓派连不到（虚拟网卡）' -ForegroundColor Yellow
    }
    if ($ok) { $reachable += $r }
}

$usable = $reachable | Where-Object { -not $_.Virtual }
if (($reachable.Count -gt 0) -and ($usable.Count -eq 0)) {
    Write-Host '  只有虚拟网卡的地址探活成功 —— 说明后端只在本机可见，' -ForegroundColor Yellow
    Write-Host '  真正的局域网地址上没有服务在听。' -ForegroundColor Yellow
}

# 推荐只从**非虚拟**地址里挑：虚拟网卡地址本机探活能过，但 Pi 连不到。
if ($usable.Count -gt 0) {
    $best = $usable | Where-Object { $_.Default } | Select-Object -First 1
    if (-not $best) { $best = $usable | Select-Object -First 1 }
} else {
    $best = $recommended
}

# ---------------------------------------------------------------- 3. 结论
Write-Head '3. 在树莓派上执行这一行'

$center = "http://$($best.Address):$Port"
Write-Host ''
Write-Host "  中心地址：$center" -ForegroundColor Green
if ($reachable.Count -eq 0 -and $listening) {
    Write-Host '  （上面探活全部失败：多半是 Windows 防火墙挡了入站，' -ForegroundColor Yellow
    Write-Host '    或后端只绑了 127.0.0.1。Pi 上用了这个地址会一直离线。）' -ForegroundColor Yellow
}
Write-Host ''
Write-Host '  ---- 复制下面这一整行到树莓派终端 ----' -ForegroundColor Cyan
Write-Host ''
Write-Host "  bash /opt/insect/InsectPestDetection/deploy/install-pi.sh --center $center --service"
Write-Host ''
Write-Host '  （这条命令可以重复执行：它会先删掉旧的中心地址再写新的，' -ForegroundColor DarkGray
Write-Host '    不会破坏已经配对好的设备。）' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  改完确认：' -ForegroundColor Cyan
Write-Host "    sudo systemctl restart insect-backend"
Write-Host "    curl -s $center/api/health"
Write-Host ''
