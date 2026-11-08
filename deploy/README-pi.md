# 树莓派 5 现场部署手册

本文把「开发机上验证过的东西」与「必须在设备上实测的东西」分开写清楚 ——
**我没有树莓派，本文中所有 Pi 专属步骤都没有在真实设备上跑过**，
已在开发机上验证的部分会逐条注明。

---

## 0. 一句话架构

```
浏览器(手机/电脑) ──HTTP/WS──►  树莓派5：FastAPI + ONNX Runtime  ──►  产出/历史/报告
                                        ▲
                                        └── 树莓派直连摄像头（本机定时抓拍）
```

采集就靠树莓派自己插的摄像头：排线摄像头（picamera2）或 USB 摄像头（OpenCV），
不需要额外的采集节点硬件。

树莓派上**不装 torch**：推理走 `onnxruntime`（ARM 上有官方 wheel），
模型是开发机上导出的 `.onnx`。

---

## 1. 硬件与系统前提

| 项 | 要求 | 原因 |
|---|---|---|
| 板子 | 树莓派 5（4GB 起，8GB 更稳） | 4 核 A76 跑 yolov8n@640 够用 |
| 系统 | **Raspberry Pi OS 64-bit**（Bookworm） | 32 位系统**没有** onnxruntime 的 wheel，装不上 |
| 电源 | 官方 27W USB-C（5V/5A） | 供电不足会降频，推理耗时翻倍且难排查 |
| 散热 | 主动散热（风扇/散热塔） | Pi 5 持续推理会热降频；被动散热撑不住 |
| SD 卡 | ≥32GB A2，或 NVMe（推荐） | 产物与数据库写 SD 卡会显著缩短寿命 |

**先确认这两条**：

```bash
uname -m            # 必须是 aarch64；显示 armv7l 说明是 32 位系统，需重装
vcgencmd measure_temp          # 温度
vcgencmd get_throttled         # 0x0 = 正常；非 0 说明发生过降频/欠压
```

`get_throttled` 非 0 时，先解决电源/散热再看性能，否则测出的帧率没有意义。

> 部署完成后，这两项在网页**「设备状态」**页随时可见（连同磁盘/内存/运行时长/重启次数与最近拍摄），
> 不必每次都 SSH 上来。页面上的 `throttled` 与 `vcgencmd get_throttled` 输出逐字一致，可对照。
> 命令仍要留着：服务没起来时只有它能用。

---

## 2. 部署步骤

### 2.0 推荐流程（三条命令）

> 正常部署走这里；2.1–2.5 是同一件事的手工拆解，排障时再看。

**① 电脑（PowerShell）**：白名单整理成一个**普通文件夹**。脚本会检查前端 `dist`
与 ONNX，缺前端时自动 `npm run build`；输出里不会出现 `app.db`、`.jwt_secret`、`*.pt`。

```powershell
.\deploy\pack-for-pi.ps1
# 默认输出：..\insect-pi-folder\InsectPestDetection\（约 48 MB）
# 换个位置：-OutDir D:\tmp
# 想直接上传：-Pi pi@<树莓派IP> -Upload（内部 scp -r）
# 盒子只当采集设备、不提供网页：加 -AgentOnly（约 38 MB）
```

用 FileZilla / WinSCP / 资源管理器把整个 `InsectPestDetection` 文件夹拖到树莓派的
`/opt/insect/` 下即可，**不需要解压**。

> 若提示“禁止运行脚本”，改用：
> `powershell -ExecutionPolicy Bypass -File .\deploy\pack-for-pi.ps1`。
> 脚本会把 `.sh` 统一转成 LF，避免 Windows 端复制导致 `bash` 报错。

**② 树莓派**：一条命令装完（依赖、`config.yaml`、systemd、启动、
健康检查、打印配对码）。

```bash
sudo mkdir -p /opt/insect && sudo chown "$USER:$USER" /opt/insect
# 把 InsectPestDetection 整个文件夹放到 /opt/insect/ 下之后：
cd /opt/insect/InsectPestDetection
bash deploy/install-pi.sh --center http://<电脑IP>:8000 --service
# CSI 排线摄像头加 --csi；只当采集设备加 --agent-only
```

> ★ **不要 `sudo bash install-pi.sh`**：脚本内部需要 root 的地方会自己调 `sudo`。
> 整个脚本以 root 跑会让 `.venv` 与运行期数据归 root，服务以普通用户启动时反而写不了。

**③ 中心网页**：登录电脑上的网页 →「我的设备」→「绑定设备」→ 输入终端打印的
6 位配对码。绑定必须在这里完成（这是账号的安全边界），盒子自己不能完成。

**改代码之后**（已装过 systemd 服务）：

```powershell
.\deploy\update-pi.ps1 -Pi pi@<树莓派IP>
# 依赖清单变了加 -WithDeps；只同步不重启加 -NoRestart
```

它只传 `backend/app`、`tools`、`migrations` 与前端 `dist`，远端用“新目录改名”
替换，不会留下已删除的旧模块。改了 `deploy/` 下的服务单元或 `config.yaml`
时，用 `pack-for-pi.ps1` 重新完整部署。

### 2.1 拷贝项目与模型

**开发机上先确认两样东西存在**（它们不在 git 里，`git clone` 是拿不到的）：

1. **前端构建产物** `vue-frontend/dist/`（`serve_frontend: true` 靠它托管页面）
2. **ONNX 模型** `weights/edge/best-yolo11s-640.onnx`（约 36 MB）

```bash
# 缺 dist 就先构建（需要 Node，仅开发机做）
cd vue-frontend && npm ci && npm run build

# 缺 ONNX 就导出（需要 torch，仅开发机做）
python run_export_model.py --model weights/best-yolo11s.pt --format onnx --imgsz 640
```

> 如果漏了 `dist/`，后端会正常启动但**网页是空的** —— 这是最常见的"上手失败"原因。
> 启动日志里会有 `serve_frontend` 相关提示；`GET /` 拿不到 HTML 就是这个问题。

> 推荐直接用 `deploy/pack-for-pi.ps1`（见 2.0）：它按**白名单**收集文件，
> 从机制上不会带上数据库、密钥与训练权重。下面的手工整理保留作排障参考。

**手工整理时不带数据集、PC 权重与开发机上的私密文件**（整个项目约 1.2 GB，其中
`datasets/` 就有 376 MB；而树莓派只用 ONNX，`.pt` 在没装 torch 的机器上反而会误导切换）。
新建一个空文件夹 `InsectPestDetection\`，只把这 13 项复制进去：

```
backend\app              backend\tests            backend\migrations
backend\requirements-edge.txt
backend\constraints.txt  backend\pytest.ini
weights\edge             deploy                   tools
README.md
vue-frontend\dist        test_media               backend\data\species_samples
```

**不要带**：`datasets/`、`runs/`、`vue-frontend/node_modules/`、`.git/`、
`outputs/`、`uploads/`、`backend/data/app.db*`、`backend/data/avatars/`、
`UserDatabase.db`、`weights/*.pt`。

> 其中两条是**安全相关**，别省：`backend/data/.jwt_secret` 是开发机的 JWT
> 签名密钥，`UserDatabase.db` 是开发机遗留的库文件。带上现场既没有用，又等于把
> 开发机的密钥散播到设备上 —— 树莓派首次启动会自己生成一份新的。

整理好后用 FileZilla / WinSCP 把整个文件夹传到树莓派的 `/opt/insect/` 下。
落地后修一次归属（路径全 ASCII，避免历史遗留的中文路径问题）：

```bash
# 用**你自己的用户名**（`id -un`），不要照抄 pi：新版 Raspberry Pi OS 不再创建
# pi 用户，chown 给一个不存在的用户会直接报 invalid user 并中断后续步骤。
# 漏掉这一步的后果是目录归 root，后面以普通用户建 .venv 会 Permission denied。
sudo chown -R "$USER:$USER" /opt/insect/InsectPestDetection
```

**关于 `backend/data/`**：输出里已包含 `species_samples/`（15 张知识库配图，360 KB，
由开发机扫描数据集生成）—— 所以**树莓派上不需要数据集也有配图**。
但 `app.db`（账号与历史）与 `avatars/` 被排除了，树莓派会**自己建一个干净的库**：

- 想保留开发机上的账号：额外拷 `backend/data/app.db` 与 `backend/data/avatars/`
- 想要全新现场环境：什么都不用做，首次启动自动建库

**「模型管理」页在树莓派上是可用的**：注册表除 `weights/*.pt` 与
`runs/detect/*/weights/best.pt` 外，也会扫 `weights/edge/*.onnx`（见
`backend/app/services/registry.py` 的候选集构造）—— 所以本包里的
`weights/edge/best-yolo11s-640.onnx` 会出现在列表里，可以直接切换。

> 注意 `.onnx` 条目**只在 `inference.backend: onnx` 下能加载**。后端不是 onnx 时
> 选中它会报错并说明原因（`routers/models.py` 里对这条做了专门提示），
> 开发机上用 torch 请选 `.pt` 权重。

**没有摄像头也能先跑通**：包里带了 `test_media/`，
把 `local_camera.backend` 设成 `simulated` 就会循环读这些图片当作采集源。

### 2.2 安装依赖

```bash
cd /opt/insect/InsectPestDetection
bash deploy/install-pi.sh

# 打算用树莓派**排线摄像头（CSI）**的话，现在就把开关加上（见 2.8）：
#   bash deploy/install-pi.sh --csi
# 事后才想起来会比较麻烦：venv 建好后没法追加这个开关，只能 rm -rf .venv 重装一遍。
#
# ★ 不要用 sudo 跑整个脚本：需要 root 的步骤它内部会自己调 sudo。
#   以 root 跑会让 .venv 与运行期数据归 root，服务以普通用户启动时写不了。
```

脚本做这些事：装系统依赖、建 `.venv`、装 `backend/requirements-edge.txt`
（**不含 torch**）、在 `config.yaml` 不存在时从模板生成、逐项列出关键依赖版本
并断言没能加载 torch。加 `--service` 时还会安装并启动 systemd 服务。

> 该脚本**未在真实 Pi 上执行过**。若 `pip` 试图源码编译 `bcrypt`，
> 说明该架构没有现成 wheel，需要 `build-essential python3-dev`（脚本已装）。

**如果 `apt-get update` 报 `将其'Origin'值从''修改到了'Debian'`**：这是 Debian 13
（trixie）安全源的 Release 元数据与 apt 本地缓存对不上。`E:` 级错误会让
`apt-get update` 返回非 0，而脚本开头是 `set -euo pipefail` —— 于是**脚本在第 1 步
就退出，`.venv` 根本不会创建**，后面手工启动会看到
`bash: .venv/bin/python: 没有那个文件或目录`。清掉缓存后重跑：

```bash
sudo rm -rf /var/lib/apt/lists/*
sudo apt-get update --allow-releaseinfo-change
cd /opt/insect/InsectPestDetection && bash deploy/install-pi.sh
```

实在绕不过 apt 时，只要 `python3-venv` 已就位就可以跳过第 1 步手工建环境
（`opencv-python-headless` 不需要 `libgl1`，缺了不影响）：

```bash
cd /opt/insect/InsectPestDetection
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r backend/requirements-edge.txt
.venv/bin/python -c "import onnxruntime, cv2, fastapi; print('依赖 OK')"
```

### 2.3 配置

`install-pi.sh` 已经会在 `config.yaml` 不存在时自动从 `deploy/config.pi.yaml`
复制一份；手工执行等价于：

```bash
cp deploy/config.pi.yaml config.yaml
```

> 用 `--center URL` 时**不需要改这个文件**：中心地址会以
> `INSECT_AGENT_CENTER_URL` 写进 systemd 单元（环境变量优先级高于 YAML），
> 同一份 `config.yaml` 可以直接部署到多台盒子。

与开发机配置的差别只有三处：`backend: onnx`、`host: 0.0.0.0`、
`target_fps: 3`（实时流上限；换用 YOLO11s 后单帧约 0.25~0.4 s，先保守取值，实测后再回调）。

### 2.4 先手工跑一次（**别急着上 systemd**）

```bash
# ★ 必须先进 backend/：`app` 这个包在 backend/ 下，从项目根启动会报
#   ModuleNotFoundError: No module named 'app'（systemd 单元的 WorkingDirectory
#   也是 backend，两边保持一致）。config.yaml 仍从项目根读取，与 cwd 无关。
cd /opt/insect/InsectPestDetection/backend
../.venv/bin/python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

另开一个终端验证：

```bash
curl -s http://127.0.0.1:8000/api/health | python3 -m json.tool
```

**打开网页**（后端已同源托管前端，盒子上不需要再跑 Vite）：

- 在树莓派本机（接了显示器）：`http://localhost:8000`
- 在同一局域网的电脑/手机上：`http://<Pi 的 IP>:8000`（`hostname -I` 查 IP）

网页里的账号是**这台盒子自己的**（存在盒子的 `app.db` 里），第一次用要先注册。
注意它和中心网页是两套独立的库：这台盒子的页面上只有**本机摄像头的状态与手动抓拍**，
定时抓拍的结果都报给了中心 —— 要看历史、看板和实时画面，请打开**中心**那台机器的网页。

**这一步是本手册的核心验收点**，期望输出：

```json
{
  "status": "ok",
  "device": "cpu",
  "backend": "onnx",          ← 必须是 onnx
  "model_loaded": true,
  "model_names": 15,
  "fallback_reason": null     ← 必须是 null
}
```

- `backend` 是 `torch` → ONNX 没生效，多半是 `onnxruntime` 没装上或模型路径不对
- `fallback_reason` 非 null → 后端发生了回退，字段里就是原因（这是部署时最容易踩的坑：
  配置写着 onnx、实际跑着 torch，只看配置文件发现不了）

再跑一次真实检测（用任意一张测试图）：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"pi","password":"pi12345"}' >/dev/null; \
  curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"pi","password":"pi12345"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s -X POST http://127.0.0.1:8000/api/detection/image \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@test_media/InsectPest_SIXU_A00025.jpg" | python3 -m json.tool
```

关注返回里的 `inference_ms` —— **这就是本机的单帧耗时**。

实测参考（2026-10，YOLO11s@640，ONNX CPU 3 线程）：开发机 **约 66 ms（≈15 FPS）**，
树莓派 5 预计慢 4~6 倍（**约 0.25~0.4 s/帧，2.5~4 FPS**）。
换了更大的模型后这个数字比早先（yolov8n 时代约 34ms）大约翻倍，属预期。

### 2.5 注册 systemd 服务

> 现在通常**不需要手动做这一节**：`bash deploy/install-pi.sh --center ... --service`
> 会自动替换路径与 `User=pi`、安装单元、启动并做健康检查。
> 下面保留手工步骤，供排障或定制时参考。

```bash
sudo mkdir -p /opt/insect
# 生成一份临时单元：替换项目路径与用户名，不修改仓库里的模板。
# ★ User=pi 必须改：新版 Raspberry Pi OS 不再创建 pi 用户，留成 pi 会让服务以
#   status=217/USER（Failed to determine user credentials）失败，而且因为
#   Restart=always 会每 5 秒重启一次、日志刷屏。已装过旧服务时先停掉：
#     sudo systemctl disable --now insect-backend
sed -e "s|/opt/insect/InsectPestDetection|$PWD|g" \
    -e "s/^User=pi$/User=$USER/; s/^Group=pi$/Group=$USER/" \
    deploy/insect-backend.service > /tmp/insect-backend.service
sudo install -m 0644 /tmp/insect-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now insect-backend
systemctl status insect-backend --no-pager
journalctl -u insect-backend -f          # 看日志
```

同样的路径/用户名替换对下面 2.6–2.7 的 `insect-watchdog.service`、
`insect-maintenance.service` 与 `field-soak.service` 也适用。

单元里刻意**没有**写 `WatchdogSec`：uvicorn 不实现 `sd_notify`，
写了会因为收不到心跳而被 systemd 反复杀掉健康进程。

### 2.6 安装看门狗

```bash
sudo cp deploy/insect-watchdog.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/insect-watchdog.sh
sudo cp deploy/insect-watchdog.service deploy/insect-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now insect-watchdog.timer
sudo systemctl start insect-watchdog.service      # 立刻跑一次
journalctl -u insect-watchdog -n 20 --no-pager
```

逻辑：每 2 分钟探测一次 `/api/health`，**连续 3 次**失败才重启服务
（单次超时可能只是推理正忙，立刻重启会打断正在跑的任务）。
它还会顺带检查 `fallback_reason` 并在日志里告警。

### 2.7 安装维护定时任务（清理产物 + 备份数据库）

```bash
sudo cp deploy/insect-maintenance.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/insect-maintenance.sh
sudo cp deploy/insect-maintenance.service deploy/insect-maintenance.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now insect-maintenance.timer
sudo systemctl start insect-maintenance.service     # 立刻跑一次
journalctl -u insect-maintenance -n 40 --no-pager
```

每天做两件事：

1. **按 `storage.retention_days`（默认 30 天）清理过期产物与上传文件** ——
   清理**只删文件、不删任务行**：历史/看板/报告要用的数字保留，
   对应的 `result_image_path` 会被置空（否则页面会去取已删除的图）。
   后端**启动时也会兜底跑一次**，所以即使忘了装定时器也不会无限增长。
2. **在线备份数据库**到 `backend/data/backups/`（默认保留 14 份）。
   用的是 SQLite 在线备份 API，**不需要停服务**、也不会拷到写入中途的不一致状态。

手工预演/核对：

```bash
.venv/bin/python tools/purge_artifacts.py --dry-run   # 只报告会清理什么
.venv/bin/python tools/backup_db.py                   # 立即备份一次
ls -lh backend/data/backups/
```

> ⚠️ 备份文件和数据库在**同一张 SD 卡**上，防的是"误删/写坏"，**不防卡坏**。
> 请定期把 `backend/data/backups/` 同步到 U 盘或另一台机器。

### 2.8 启用树莓派本机摄像头（推荐：不需要额外硬件）

这一路让**盒子自己插着的摄像头**按间隔自动抓拍 —— 这是本项目唯一的采集方式，不需要额外硬件。

**装依赖（按摄像头类型二选一）**

```bash
# 排线摄像头（Pi Camera Module，CSI）—— 用 Pi OS 官方的 picamera2
sudo apt-get install -y python3-picamera2
# 注意：picamera2 必须装到**系统 Python**（apt），pip 装进 venv 通常拿不到 libcamera 的绑定。
# 默认的 .venv 是隔离的，看不见系统包，所以 import picamera2 会失败。
# 用安装脚本的 --csi（等价于 --system-site-packages）开关重建（脚本会先查
# pyvenv.cfg，已经开过这个开关就直接跳过，不会白装一遍依赖）：
#   rm -rf .venv && bash deploy/install-pi.sh --csi
# 只插 USB 摄像头的话不要加这个开关 —— 隔离的 venv 更干净。

# USB 摄像头 —— 不需要额外包，opencv 已在 requirements-edge.txt 里
ls /dev/video*        # 应能看到 /dev/video0
```

**配置**（`deploy/config.pi.yaml` 里已默认启用）

```yaml
local_camera:
  enabled: true       # 启动后端时自动开始定时抓拍
  backend: auto       # auto 优先 picamera2，其次 opencv
  device: "0"         # USB 摄像头用；picamera2 忽略
  interval_min: 30
```

**验证**

```bash
curl -s http://127.0.0.1:8000/api/local-camera/status -H "Authorization: Bearer <令牌>" | python3 -m json.tool
```

关注三处：`backend` 是不是你插的那种、`running` 是否 true、
`available` 里每个图像源的 `detail`（不可用时会写明原因和安装命令）。

也可以打开网页 → **设置** → 「本机摄像头（边缘盒子自采）」面板：
能看到状态、点「立即抓拍」、启停定时抓拍、展开「可用图像源」排障。
抓拍结果会像其它任务一样进入**历史、看板与报告**（`mode=local`）。

**排障**

| 现象 | 原因与处理 |
|---|---|
| `picamera2 available=false` | 没装或用 apt 装但 venv 看不到 → 见上面 `--system-site-packages` 的做法 |
| 抓拍报「取帧失败」 | 设备被别的程序占用（例如桌面上的拍照程序），或被拔掉 |
| 图像全黑/严重偏色 | CSI 摄像头刚启动时 AGC/AWB 未收敛 → 调大 `warmup_frames`（默认 5） |
| 没有摄像头也想先跑通 | `backend: simulated`，从 `simulated_dir`（默认 `test_media`）循环读图 |

**注意**

- 不要把 `interval_min` 设得太小：抓拍（尤其 picamera2 启动）与推理都要 CPU，
  间隔过密会持续占满 4 核、温度上升触发降频。现场建议 **≥5 分钟**。
- 摄像头同一时刻只能被一个程序独占：本机摄像头与「网页实时检测」用的是不同来源，
  但若你另开了 `libcamera-still` 之类的程序，会互相抢设备。
- **未在真实树莓派上验证过**：`simulated` 源已在本机端到端验证
  （定时触发、落库、进历史/看板/报告、停止释放设备），
  但 `picamera2` 与 USB 两路只做了代码层面的对接，需你在设备上按上面三处确认。

### 2.9 把盒子绑到账号上：远程看识别结果与实时画面

到这一步为止，这台 Pi 是**自给自足**的：网页、后端、数据都在它自己身上，你必须在
同一个局域网里打开它才知道田里的情况。这一节把 Pi 变成**挂在账号下的远程设备** ——
在别处（家里的电脑、以后的公网服务器）登录同一个账号，就能看到这台盒子的
**实时摄像头画面**和它每次定时抓拍的**识别结果**。

**先理解它的形状，再动手**（决定了后面所有操作）：

```
    中心（跑网页的那台电脑/服务器）          树莓派（田里）
        从不主动连盒子  ◄──────────────  盒子主动拨出去
                       ① 配对、定时上报结果（HTTP）
                       ② 推实时画面（WebSocket，原始 JPEG）
```

所有连接都是**盒子主动拨出去**的。所以：

- 不需要给盒子做端口映射、frp、内网穿透 —— 只要盒子能连通中心的地址；
- 从局域网换到公网，**只改 `center_url` 这一个值**，前端和中心代码一个字都不用改；
- 中心挂了，盒子照常抓拍和推理，结果排队重发（进程内最多 8 条，按 `seq` 幂等）。

#### 前置条件

1. **一台中心**：已经在跑本项目后端、且你能在浏览器里打开它并登录。
   记下它的地址，例如 `http://192.168.1.20:8000`。
2. **盒子能连通中心**：

   ```bash
   curl -s http://192.168.1.20:8000/api/health
   ```

   拿不到 `{"status":"ok"...}` 就先解决网络，别往下走 —— 后面每一步都依赖它。

#### 步骤一：在盒子上启用代理

> **更省事的方式**：安装时直接
> `bash deploy/install-pi.sh --center http://192.168.1.20:8000 --service`。
> 脚本会把 `INSECT_AGENT_ENABLED=1` 与 `INSECT_AGENT_CENTER_URL` 写进 systemd
> 单元，不需要手工改 `config.yaml`。下面两种是手工等价操作。

改 `config.yaml` 的 `agent:` 块（字段含义见 `deploy/config.pi.yaml` 里的注释）：

```yaml
agent:
  enabled: true
  center_url: "http://192.168.1.20:8000"   # ★ 中心的地址，不是本机地址
  name: "东头盒子"                          # 网页上显示的名字
```

或者不改配置文件，用 systemd 的环境变量（同一份配置跑多台盒子时更方便）：
把 `deploy/insect-backend.service` 里那两行 `#Environment=INSECT_AGENT_*` 的
`# ` 去掉，然后：

```bash
sudo systemctl daemon-reload && sudo systemctl restart insect-backend
```

> **启用代理会取代本机的定时抓拍。** 摄像头只能被一个进程打开，两个调度器各抓各的
> 会让同一帧被推理两次、每个间隔产出两条任务。代理复用的正是本机摄像头那套取帧与推理，
> 只是把结果发给了中心。所以 `local_camera.enabled` **保持 `true`** —— 它是代理的取帧来源。

#### 步骤二：读出配对码

> 用 `install-pi.sh --service --center ...` 安装时，脚本会在启动后自动读出这个码
> 并打印在终端上，不需要再翻日志。

盒子会立刻向中心要一个 6 位配对码，并**醒目地打进日志**：

```bash
journalctl -u insect-backend -f
```

```
============================================================
   设备配对码： 381402
   去网页「我的设备 → 绑定设备」输入它完成绑定
   有效期 5 分钟；过期后这里会自动打印一个新的
============================================================
```

> 这个方框是用 **WARNING 级别**的日志打出来的（不是 `print`），这一点是刻意的 ——
> 正因为本项目只有 WARNING 及以上能进日志（下一段解释），用 INFO 打就会**什么都看不到**。

看漏了也没关系，同一份码也写在文件里：

```bash
cat /opt/insect/InsectPestDetection/backend/data/agent_pairing.txt
```

> **配对码只是给人抄的，盒子自己另外拿了一枚凭证。** 中心在发码的同时下发一枚
> 高熵的 `pairing_secret`（`X-Pairing-Secret` 头，跟着 `agent.json` 一起落盘），
> 之后每次轮询都要带回来。所以**只知道 `hardware_id`（Pi 的 serial，
> `/proc/cpuinfo` 里就有）的人拿不到任何东西** —— 他既领不走设备令牌，
> 也确认不了这台盒子是不是正在配对。而且令牌只认**绑定那一刻**的那枚凭证：
> 就算有人在绑定完成之后又对同一台硬件发一次 `pair/start`（他能顶掉屏幕上的码，
> 这是首次接触挡不住的），**领取资格也换不掉** —— 令牌仍然只交给这里这台盒子。
> 这件事由代理自动完成，不需要人工介入；
> 但要留意：**这一条要求中心和盒子是配套的版本**，老代理不带这个头会被一直回
> `unknown`，它在日志里会明说「中心与代理的版本不一致，两边都要升级」而不是空转。

> ⚠️ **别在日志里找代理的常规动静 —— 找不到。**
> 本项目没有配置 Python 的日志 handler，uvicorn 只配了它自己那几个 logger，
> 于是应用自己的日志落到解释器的兜底 handler 上，**只有 WARNING 及以上会被打出来**。
> 代理的 `边缘代理已启动`、`配对成功`、`抓拍完成`、`上报成功` 全是 INFO 级，
> **journalctl 里一行都不会有**；能看到的只有 WARNING 及以上 ——
> 也就是上面那个配对码方框，以及出问题时的告警
> （例如 `设备令牌被中心拒绝（403）：清掉本地令牌，回到配对`）。
>
> 所以**排障主要看中心网页**（「我的设备」页的在线状态与最近上报时间、设备详情页的
> 画面帧率），日志只用来看"有没有报错"。这是本项目的既有问题，不是代理引入的，
> 记录在 `README.md` 的「已知问题与限制」里。

接口 `GET /api/system/agent` 也能查代理状态与配对码（含上报/推流计数与最近错误），
这是日志不可用现状下**唯一能看到代理内部计数的入口**；
`POST /api/system/agent/live-detection`（body `{"enabled": true|false}`）切换推流画面的
实时识别，设置页上的开关调的就是它。但它和 `/api/system/host`
一样需要登录，命令行直接 `curl` 会拿到 401：

```bash
# 需要先在这台盒子上登录拿令牌，curl 裸调是不行的
curl -s http://127.0.0.1:8000/api/system/agent \
     -H "Authorization: Bearer <在这台盒子上登录后的令牌>" | python3 -m json.tool
```

> **配对码在界面上仍然没有入口。** 设置页现在有一张「实时识别」卡片，会显示代理是
> 「推流中 / 未推流」；但配对码与上报计数还没有展示位 —— 所以拿配对码实际可用的两条路
> 仍然是**启动日志里那个方框**（WARNING 级，能进日志）和上面的 `agent_pairing.txt`
> （直接写文件，与日志系统无关）。这个缺口记在 README 的「下一步」里。

#### 步骤三：在中心网页上绑定

登录中心 → 侧栏「**我的设备**」→「**绑定设备**」→ 把 6 位码填进去 → 绑定。

成功后会直接进设备详情页，应该能看到：

- **实时画面**：现场画面按 `agent.stream_fps`（默认 2 帧/秒）持续播放。
  默认这一路是**原始画面、不画框** —— 盒子的 CPU 要留给定时抓拍。
  想边看边识别，打开这台盒子自己的网页 `http://<Pi>:8000/settings`，在
  「实时识别（推流画面）」卡片里打开开关：此后每一帧都在 Pi 上推理并画框，
  推流自动限制为 2 帧/秒；开关写入 `backend/data/agent.json`，重启后保留。
  实时推理持续占 CPU、可能让机身升温，不用时关掉即可。
  这个开关**只在盒子本机**有 —— 中心不能反向控制盒子。
- **检测结果**：盒子每 `capture_interval_min`（默认跟随 `local_camera.interval_min`，
  现场建议 ≥5 分钟）抓拍一次并在**本机推理**，把**原图 + 框坐标**报给中心，
  标注图由中心自己画。这些结果同时进入历史页、看板和报告（模式显示为「设备上报」）。

绑定成功后 `data_dir/agent_pairing.txt` 会被删掉，令牌落盘到 `data_dir/agent.json`，
**盒子重启不需要重新配对**。

> 同一台机器既当中心又当盒子（`center_url` 填 `127.0.0.1`）技术上可行，
> 但没有任何意义 —— 你在本地看它本机就够了。这种方式只用来在没有树莓派时
> 验证整条链路（见 §7）。

#### 解绑

在设备详情页点「**解绑**」。盒子会在下一次心跳（≤10 秒）时被拒，
自己清掉本地令牌并**重新打印一个新的配对码**，等着被绑到别的账号。
已上报的历史结果**不会**被删除 —— 解绑是权限变更，不是数据销毁。

已经打开的实时画面也会自己停下：流接口每隔 30 秒复查一次归属，
**最多 30 秒**之后那一页就不再刷新，不会出现「解绑了但别人的页面还在看这台摄像头」。
（这个上限是 `routers/devices.py` 的 `STREAM_REVALIDATE_S`，调小会让查库更频繁。）

#### 排障

| 现象 | 原因与处理 |
|---|---|
| 日志里没有配对码 | 先确认 `agent.enabled` 真的生效：**`agent.json` 里有没有内容**（`cat backend/data/agent.json`，没有这个文件就是代理压根没跑）。有文件却没码，就是盒子拨不通中心（见前置条件 2） |
| 日志反复「中心返回 HTTP 500」 | **中心**的库比代码旧：`create_all` 只建缺失的表、不给已有的表补列。在中心执行 `tools/migrate_add_devices.py --apply` 和 `tools/migrate_add_ownership.py --apply`，再重启中心后端 |
| 输入码报「配对码无效或已过期」 | 码只有 5 分钟。回盒子看**新**码：`cat backend/data/agent_pairing.txt`（会自动换一个） |
| 输入码报「该设备已被绑定」 | 这台盒子已属于某个账号。先在那个账号里解绑，再配对 |
| 输入码报「尝试次数过多」 | 连错多次后的限流（每人 10 次/10 分钟）。等 10 分钟再试 |
| 绑上了但一直显示「离线」 | 盒子到中心的连接没建立：确认中心地址从盒子上真的能拨通（先在盒子上 `curl <中心地址>/api/health`）。盒子侧的失败原因会以 **WARNING** 打进日志（这是少数能在 `journalctl` 里看到的东西） |
| 有检测结果但**没有画面** | `agent.stream_enabled` 是否为 `true`；画面区会写明「盒子在线但还没有画面帧」 |
| 解绑后盒子像没反应 | 最多等一次心跳（10 秒）。盒子要等到被拒才会去重新配对 |
| 换了新码但网页上没变 | 网页上的「我的设备」每 10 秒刷新一次，等一下或点「刷新」 |

> 上表刻意不依赖日志：**代理的常规 INFO 日志在本项目里是看不到的**（原因见步骤二里
> 那个警告框）。判断"到底跑起来没有"最可靠的两个信号是
> `backend/data/agent.json` **在不在**，和中心网页上**在线状态 + 最近上报时间**有没有在动。

#### 这一节的局限

- **只做了局域网**。换成公网需要给中心加 HTTPS/wss、限流与 CORS 白名单 ——
  这些**明确推迟**了，代码里没做。只外拨的形状让"换地址"不用改代码，
  但随之而来的安全加固不是自动的。
- **实时画面用短期票据**（120 秒）放在 URL 的 query 里。这是 `<img>` 无法设置
  请求头的无奈之举，已经用「短寿命 + 限定用途 + 限定设备」把损害收敛到
  「看这一路摄像头两分钟」，但它**确实**会进中间设备的访问日志。
- **代理跑在盒子自己的后端进程里**，所以盒子必须跑完整后端（对本项目现有部署是对的，
  但不是那种 200MB 的瘦客户端镜像）。
- **未在真实树莓派上验证过**：配对、上报、推流三条链路已在开发机上用两个真 uvicorn
  实例端到端跑通过（**逐项证据见 §7**），但那些用的是 `simulated` 源。
  picamera2 在"推流取帧"与"定时推理"两路交替调用下的行为仍需在设备上确认 ——
  这是整个方案里**最未经实测**的一条。

---

## 3. 性能与功耗实测（阶段 C 的收尾，必须在设备上做）

### 3.1 帧率

不要用「感觉」，用同一张图重复测：

```bash
cd /opt/insect/InsectPestDetection
.venv/bin/python tools/verify_onnx_parity.py run --backend onnx \
    --model weights/edge/best-yolo11s-640.onnx --out outputs/pi_onnx.json
```

输出的每行末尾是耗时；脚本同时会打印后端信息。跑 3 次取中位数。

若单帧明显慢于预期，按顺序查：① `get_throttled`/温度；② `INSECT_ONNX_THREADS`
是否被设得过小；③ 是否误用了 640 以外的 imgsz（改小 imgsz 可显著提速，
但需重新导出并**重跑一致性验证**）。

### 3.2 摄像头实时流能到多少 fps

把 `config.yaml` 的 `stream.target_fps` 从 3 往上调，逐个试，用页面上的
实际 fps 与「丢帧提示」判断上限。**后端是单实例串行推理**，
设得超过实际能力只会让帧堆积后被丢弃，不会变快。

### 3.3 功耗

```bash
# 需要带功率计的 USB-C 线，或读 PMIC
vcgencmd pmic_read_adc | grep -E 'EXT5V_V|VDD_CORE_A'
```

记录三种状态：空闲、单帧推理、实时流跑满。**7×24 部署前必须确认
电源与散热在连续满载下不会降频。**

---

## 4. 现场网络

### 4.1 让手机能访问

`config.yaml` 已设 `host: 0.0.0.0`，但还需知道板子的地址：

```bash
hostname -I
```

手机浏览器打开 `http://<板子IP>:8000`。

### 4.2 没有路由器的田间：自建热点

Pi OS Bookworm 用 NetworkManager：

```bash
sudo nmcli device wifi hotspot ifname wlan0 ssid insect-pi password 'change-me-8+'
sudo nmcli connection modify Hotspot ipv4.method shared
```

> 该命令**未在真实设备上验证**，请以 `nmcli connection show` 的实际结果为准。

⚠️ **一个必须知道的限制**：浏览器只在**安全上下文**（https 或
`localhost`/`127.0.0.1`）下开放摄像头。手机通过
`http://192.168.x.x:8000` 访问时，网页里的「开启摄像头」会不可用
（页面现在会明确提示原因）。现场要用手机摄像头，需满足其一：

- 在板子上配 HTTPS（自签证书 + 手机信任，或内网 CA）
- 或让手机也连到同一台机器并用 `localhost` 访问（仅限在板子本机操作）

（原先还有一条"设备主动上传"的路，已随设备接入层移除。）

### 4.3 固定地址

现场建议在路由器上做 DHCP 绑定，或给板子配静态地址，避免每次开机换 IP。

---

## 5. SD 卡寿命与断电安全

7×24 写 SD 卡会显著缩短寿命（数据库 WAL + 产物写入）。三种做法，按推荐度排：

1. **换 NVMe 启动**（Pi 5 支持）：最彻底，强烈推荐
2. **挂 overlayfs 只读根文件系统**：`sudo raspi-config` →
   Performance Options → Overlay File System → 启用。
   代价是系统盘改动不持久，运行期数据必须放在可写分区（`outputs/`、`backend/data/`）
3. **限制产物增长**：已实现 —— `storage.retention_days`（默认 30 天）由
   `deploy/insect-maintenance.timer` 每天执行一次，后端启动时也兜底跑一次（见 §2.7）。
   同一定时任务还会**每天在线备份数据库**到 `backend/data/backups/`（默认保留 14 份）。

另外记得：**SQLite 已开 WAL**，突然断电比正常关机更容易损坏数据库。
条件允许就接一个小 UPS（Pi 5 功耗不高，便宜的行动电源即可）。

---

## 6. 故障排查

| 现象 | 先查什么 |
|---|---|
| `/api/health` 的 `backend` 是 torch | `onnxruntime` 是否装上；`fallback_reason` 字段里写着具体原因 |
| 服务起不来 | `journalctl -u insect-backend -n 50 --no-pager`；多半是模型路径或 config.yaml 写法 |
| 推理特别慢 | 先看网页**「设备状态」**页的降频/欠压与温度（不用 SSH 上去敲 `vcgencmd`）；再查电源是否够、`INSECT_ONNX_THREADS` |
| 手机打不开页面 | `host` 是否 0.0.0.0；防火墙/路由器是否隔离了客户端 |
| 网页「开启摄像头」灰色 | 见 4.2：需要 https 或 localhost（页面会显示原因） |
| 数据库锁死/损坏 | 是否断电过；`sqlite3 backend/data/app.db "PRAGMA integrity_check;"` |

---

## 7. 开发机上已验证 vs Pi 上待验证

**已验证（在 Windows 上，用与 Pi 相同的免 torch 依赖集）**

- ONNX 后端可加载并完成真实推理；`/api/health` 如实报告 `backend: onnx`、`device: cpu`
- **全程未导入 torch / ultralytics**（这正是 Pi 能跑的前提）
- letterbox 预处理与 ultralytics 参考实现**逐像素一致**（6 种形状，最大像素差 0）
- 与 torch 后端的一致性：5/6 用例 IoU ≥ 0.967；1 个极端用例（800×300，
  上下各补 200 像素灰边）IoU 0.867 —— 类别相同、分数相差 0.0003，
  属 ONNX Runtime 与 PyTorch 的数值差异，已确认**不是**预处理错误
- `backend: onnx` 缺 `onnxruntime` 时**会**回退 torch 并记录原因（此前不会，已修）

**设备接入链路（§2.9）在本机两个实例上端到端跑通过**（2026-10-16，无树莓派）

同一台机器上起了**两个真 uvicorn 实例**：一个当中心（`serve_frontend: true`），
一个当盒子（`agent.enabled: true` + `center_url` 指向中心 + `local_camera.backend: simulated`）。
逐项观察到的事实：

- 盒子在日志里打出 WARNING 级的配对码方框；用该码在中心 `POST /api/devices/bind` → **200**
- 绑定后设备列表出现该设备，`online: true`、`stream_fps: 2.01`（与配置的 2 fps 一致）
- **实时画面是真在出图**：`GET /devices/{id}/stream?ticket=...` 返回
  `multipart/x-mixed-replace; boundary=ipframe`，带 `Referrer-Policy: no-referrer`；
  3 秒收到 6 帧，逐帧 `cv2.imdecode` **全部可解码**（416×416 JPEG）
- 盒子的定时抓拍经本机推理后上报成功，中心落了 `mode="device"`、`device_id=1` 的任务，
  接口返回的标注图地址带媒体票据（`?mt=...`）且可解码；**裸地址 `/media/outputs/...`
  现在回 401**（`media_guard.py`）
- `GET /api/tasks?device_id=1` 与 `GET /api/stats/summary?device_id=1` 生效；
  **不传该参数时是"我看得见的全部"**（有主的只有主人看得见，无主的公共）
- **归属隔离**：第二个用户看不到该设备，访问它的详情/任务/领票/解绑**一律 404 而不是 403**，
  用 `device_id` 过滤旁路读也**被拒（404）**
- **票据是绑定设备的**：设备 1 的票据取设备 2 的流 → 401；乱写的票据 → 401
- **只报 `hardware_id` 领不走令牌**：绑定的同一个时刻，不带 `X-Pairing-Secret`
  （或带一个乱写的）去 `pair/poll` 拿到的是 `unknown`、没有 `device_token`；
  真盒子随后带凭证去**一次**就拿到了。凭证也随 `agent.json` 一起落盘，
  所以配到一半重启不会把它弄丢
- **解绑**：设备转 `unbound`、从列表消失；盒子收到 **403**（不是 401）后清掉本地令牌、
  重新以 WARNING 打出**新**配对码，`agent.json` 里的 `device_token` 已清空而
  `seq` 继续累加；用新码重新绑定 → 再次上线
- SPA 深链 `/devices/1` 返回真正的 `index.html`；路径穿越 `/%2e%2e/%2e%2e/config.yaml` 仍 404

**后端测试基线**

- 带 torch 的开发环境（`backend/requirements.txt` 那套）：**351 passed, 1 skipped**（共 352 项）
- 免 torch 的 `.venv-edge`：**121 passed / 44 error / 6 failed / 8 skipped**。
  那 50 项失败**全部**是 `BackendUnavailableError: 未安装 ultralytics`（凡是真要加载
  TorchBackend 的用例都会这样），是缺 torch 的**预期结果，不是回归** ——
  在 Pi 上排查问题时不要拿 `.venv-edge` 的 pytest 结果当基线

**待验证（必须在真实 Pi 上做）**

- `deploy/install-pi.sh` 能否一次装通（aarch64 上各 wheel 是否齐全）
- systemd 单元与看门狗在真实 systemd 下的行为
- 实际帧率、功耗、温度与是否降频
- 热点配置与手机访问
- 长时间运行下的稳定性（阶段 E）
- **picamera2 在"推流取帧"与"定时推理"两路交替调用下的行为**（§2.9 的功能）：
  上面那次端到端跑的是 `simulated` 源，取帧只是一次次读文件；CSI/USB 摄像头
  被两路以差得很远的节奏（2 fps 取帧 vs 每 N 分钟一次推理）交替 `capture_array()`，
  是本方案里**最未经实测**的一条。若真出问题，缓解办法是在 `LocalCameraService` 里
  加一个专用取帧线程供两路拉取（改动很小、很局部）

---

## 8. 下一步

1. 在 Pi 上跑通 2.4 的验收点（`backend: onnx` + `fallback_reason: null`）
2. 确认本机摄像头可用（`/api/local-camera/backends` 里 CSI 或 USB 源 `available=true`），
   并定下现场抓拍间隔（`local_camera.interval_min`，建议 ≥5 分钟）
3. 按 §2.9 把盒子绑到账号上，**在真实摄像头上确认"推流取帧"与"定时推理"两路能同时工作**
   （这是设备接入里唯一没法在开发机上验的部分）
4. 实测帧率与功耗，回填 `stream.target_fps`
5. 田间 7×24 验证：按 `docs/田间验证记录模板.md` 逐项记录（断网、断电、摄像头故障、温度功耗）
