#!/usr/bin/env bash
# ==========================================================
# 病虫害智能监测系统 —— 树莓派（Raspberry Pi OS 64-bit）部署脚本
#
#   bash deploy/install-pi.sh [选项]
#
# 选项：
#   --center URL        把这台盒子挂到中心（例如 http://192.168.1.20:8000）。
#                       只写进 systemd 环境变量，不改 config.yaml。
#   --service           安装并启动 systemd 服务（自动替换路径与 User=pi）
#   --csi               等价于 --system-site-packages（CSI 排线摄像头）
#   --system-site-packages
#   --agent-only        只当采集设备：不检查 vue-frontend/dist
#   --name NAME         设备在中心网页上显示的名字（默认 hostname）
#   --user USER         服务运行用户（默认当前用户，`id -un`）
#   -h, --help
#
# 默认行为与从前一致：只装依赖、建目录，不动 systemd。
# --service 才会写 /etc/systemd/system/insect-backend.service 并启动它。
#
# ★ 请用**普通用户**运行，不要 sudo 整个脚本：
#     bash deploy/install-pi.sh --center http://192.168.1.20:8000 --service
#   脚本内部需要 root 的地方会自己调 sudo。整个脚本以 root 跑会让 .venv
#   和现场数据归 root，之后服务以普通用户运行时反而写不了。
#
# 加 --system-site-packages 时，.venv 会带上同名开关，从而能看见 apt 装到
# **系统 Python** 的包。需要它的唯一场景是树莓派排线摄像头：python3-picamera2
# 只能由 apt 安装（libcamera 的 Python 绑定不在 PyPI 上），默认的隔离 venv 里
# `import picamera2` 会失败。只插 USB 摄像头的话不需要这个开关。
#
# 不做的事（避免脚本越权改坏现场）：
#   * 不装看门狗/维护定时器 —— 见 deploy/README-pi.md 的 §2.6 / §2.7
#   * 不改网络/热点 —— 见 README-pi.md 的「现场网络」一节
#
# 为什么不用 torch：树莓派上 torch 的 wheel 体积大、ARM 上装 CUDA 版不可行。
# 推理走 onnxruntime（见 backend/requirements-edge.txt）。
# ==========================================================
set -euo pipefail

WITH_SYSTEM_SITE=0
INSTALL_SERVICE=0
AGENT_ONLY=0
CENTER_URL=""
AGENT_NAME=""
RUN_USER=""

usage() {
  # 打印文件头部的注释块：从第 3 行到闭合的 `# ====`，去掉每行开头的 "# "。
  sed -n '3,/^# ====/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --system-site-packages|--csi)
      WITH_SYSTEM_SITE=1
      shift
      ;;
    --service)
      INSTALL_SERVICE=1
      shift
      ;;
    --agent-only)
      AGENT_ONLY=1
      shift
      ;;
    --center)
      [[ $# -ge 2 && -n "${2:-}" ]] || { echo "!! --center 缺少地址" >&2; exit 2; }
      CENTER_URL="$2"
      shift 2
      ;;
    --center=*)
      CENTER_URL="${1#*=}"
      shift
      ;;
    --name)
      [[ $# -ge 2 ]] || { echo "!! --name 缺少值" >&2; exit 2; }
      AGENT_NAME="$2"
      shift 2
      ;;
    --name=*)
      AGENT_NAME="${1#*=}"
      shift
      ;;
    --user)
      [[ $# -ge 2 && -n "${2:-}" ]] || { echo "!! --user 缺少用户名" >&2; exit 2; }
      RUN_USER="$2"
      shift 2
      ;;
    --user=*)
      RUN_USER="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数：${1}（用 --help 查看用法）" >&2
      exit 2
      ;;
  esac
done

if [[ -n "${CENTER_URL}" ]]; then
  # 末尾斜杠会让拼出来的 /api/... 变成 //api/...；虽然多半也能通，但先收掉。
  CENTER_URL="${CENTER_URL%/}"
  if [[ ! "${CENTER_URL}" =~ ^https?:// ]]; then
    echo "!! --center 必须以 http:// 或 https:// 开头，收到：${CENTER_URL}" >&2
    exit 2
  fi
  if [[ "${CENTER_URL}" =~ ^https?://(127\.0\.0\.1|localhost)([:/]|$) ]]; then
    echo "!! --center 填的是本机地址。这里要填**中心（跑网页的电脑）**的地址；"
    echo "   盒子上的 8000 端口是它自己，不是中心。"
  fi
fi

# 整个脚本以 root 运行会把 .venv 与运行期目录变成 root 所有，
# 之后 systemd 以普通用户启动时写不了 backend/data。直接拒绝，比事后 chown 干净。
if [[ "${EUID}" -eq 0 ]]; then
  echo "!! 请不要用 sudo 运行整个脚本（会让 .venv 归 root）。" >&2
  echo "   改用：bash $0 $*" >&2
  echo "   脚本需要 root 的步骤（apt / systemd）会自己调用 sudo。" >&2
  exit 1
fi

run_as_root() {
  sudo "$@"
}

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
PY="${VENV_DIR}/bin/python"

echo "==> 项目目录：${PROJECT_DIR}"

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "!! 当前架构是 $(uname -m)，本脚本面向 64 位树莓派系统（aarch64）。"
  echo "   32 位系统上 onnxruntime 没有可用 wheel，请改用 64 位 Raspberry Pi OS。"
  read -r -p "   仍要继续吗？(y/N) " reply
  [[ "${reply}" == "y" || "${reply}" == "Y" ]] || exit 1
fi

echo "==> 1/5 安装系统依赖"
# libgl1 / libglib2.0-0：opencv 运行需要（即便用 headless 版，部分发行版仍会链接）
run_as_root apt-get update
run_as_root apt-get install -y --no-install-recommends \
  python3-venv python3-dev build-essential \
  libgl1 libglib2.0-0 libopenblas0 \
  curl

echo "==> 2/5 创建虚拟环境并安装 Python 依赖（不装 torch）"

has_system_site() {
  [[ -f "${VENV_DIR}/pyvenv.cfg" ]] || return 1
  grep -q "^include-system-site-packages = true" "${VENV_DIR}/pyvenv.cfg"
}

if [[ ! -x "${PY}" ]]; then
  # ${VENV_FLAGS} 不加引号是有意的：为 0 时它是空串，不能让空参数传给 venv
  VENV_FLAGS=""
  [[ "${WITH_SYSTEM_SITE}" == "1" ]] && VENV_FLAGS="--system-site-packages"
  # shellcheck disable=SC2086
  python3 -m venv ${VENV_FLAGS} "${VENV_DIR}"
elif [[ "${WITH_SYSTEM_SITE}" == "1" ]] && ! has_system_site; then
  # 已建好的 venv 没法"追加"这个开关，只能重建。与其静默地按无开关继续跑
  # （后面 import picamera2 再报一个看不懂的错），不如在这里停下说清楚。
  echo "!! 现有的 ${VENV_DIR} 未开启 --system-site-packages，必须重建：" >&2
  echo "     rm -rf ${VENV_DIR} && bash $0 --csi" >&2
  exit 1
fi
"${PY}" -m pip install --upgrade pip
"${PY}" -m pip install -r "${PROJECT_DIR}/backend/requirements-edge.txt"

echo "==> 3/5 创建运行期目录并准备配置"
mkdir -p "${PROJECT_DIR}/backend/data" "${PROJECT_DIR}/outputs" "${PROJECT_DIR}/uploads"

if [[ -f "${PROJECT_DIR}/config.yaml" ]]; then
  echo "    config.yaml 已存在，保持不动"
else
  cp "${PROJECT_DIR}/deploy/config.pi.yaml" "${PROJECT_DIR}/config.yaml"
  echo "    已从 deploy/config.pi.yaml 生成 config.yaml"
fi

MODEL="${PROJECT_DIR}/weights/edge/best-yolo11s-640.onnx"
if [[ ! -f "${MODEL}" ]]; then
  echo "!! 缺少 ONNX 模型：${MODEL}"
  echo "   推理会退回/失败。在开发机上导出后重新打包："
  echo "     python run_export_model.py --model weights/best-yolo11s.pt --format onnx --imgsz 640"
fi

if [[ "${AGENT_ONLY}" -eq 0 && ! -f "${PROJECT_DIR}/vue-frontend/dist/index.html" ]]; then
  echo "!! 缺少 vue-frontend/dist —— 这台盒子自己的网页打不开（API 仍可用）。"
  echo "   在开发机上构建后重新打包：cd vue-frontend && npm run build"
  echo "   如果只把盒子当采集设备，加 --agent-only 即可忽略。"
fi

echo "==> 4/5 校验安装结果"
"${PY}" - <<'PY'
import importlib, sys
missing = []
for name in ("onnxruntime", "cv2", "numpy", "yaml", "fastapi", "sqlalchemy", "jwt", "bcrypt", "openpyxl", "docx", "PIL"):
    try:
        module = importlib.import_module(name)
        print("  %-14s OK   %s" % (name, getattr(module, "__version__", "?")))
    except Exception as exc:
        missing.append(name)
        print("  %-14s 缺失 (%s)" % (name, type(exc).__name__))
if "torch" in sys.modules:
    print("  !! 竟然加载了 torch，边缘环境不该有它")
if missing:
    print("\n缺失依赖：", ", ".join(missing))
    raise SystemExit(1)
print("\n依赖齐备（且未引入 torch）")
PY

# ---------------------------------------------------------- 5/5 systemd
if [[ "${INSTALL_SERVICE}" -eq 1 ]]; then
  echo "==> 5/5 安装并启动 systemd 服务"

  if [[ -z "${RUN_USER}" ]]; then
    RUN_USER="$(id -un)"
  fi
  if [[ "${RUN_USER}" == "root" ]]; then
    echo "!! 服务不能以 root 运行。请加 --user <你的用户名>。" >&2
    exit 1
  fi
  if ! id -u "${RUN_USER}" >/dev/null 2>&1; then
    echo "!! 用户 ${RUN_USER} 不存在（用 id -un 查当前用户名）。" >&2
    exit 1
  fi

  UNIT_SRC="${PROJECT_DIR}/deploy/insect-backend.service"
  UNIT_TMP="$(mktemp)"
  # sed 的替换串里这三个字符有特殊含义，路径/用户名里出现时要转义。
  escape_sed() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
  PROJECT_ESC="$(escape_sed "${PROJECT_DIR}")"
  RUN_USER_ESC="$(escape_sed "${RUN_USER}")"

  sed -e "s|/opt/insect/InsectPestDetection|${PROJECT_ESC}|g" \
      -e "s|^User=pi$|User=${RUN_USER_ESC}|" \
      -e "s|^Group=pi$|Group=${RUN_USER_ESC}|" \
      "${UNIT_SRC}" > "${UNIT_TMP}"

  if ! grep -q '^ExecStart=' "${UNIT_TMP}"; then
    rm -f "${UNIT_TMP}"
    echo "!! 单元模板里没有 ExecStart，无法安装：${UNIT_SRC}" >&2
    exit 1
  fi

  if [[ -n "${CENTER_URL}" ]]; then
    # 先删掉可能已经存在的活动配置行（重复的 Environment= 以后者为准，
    # 但留着旧地址会让人误以为改这里生效）。
    sed -i -E '/^Environment="?INSECT_AGENT_(ENABLED|CENTER_URL|NAME)=/d' "${UNIT_TMP}"
    CENTER_ESC="$(escape_sed "${CENTER_URL}")"
    # 依次插在 ExecStart 前；插入顺序与最终顺序相反，所以先 name、再 center、最后 enabled。
    if [[ -n "${AGENT_NAME}" ]]; then
      NAME_ESC="$(escape_sed "${AGENT_NAME}")"
      sed -i "/^ExecStart=/i Environment=\"INSECT_AGENT_NAME=${NAME_ESC}\"" "${UNIT_TMP}"
    fi
    sed -i "/^ExecStart=/i Environment=INSECT_AGENT_CENTER_URL=${CENTER_ESC}" "${UNIT_TMP}"
    sed -i "/^ExecStart=/i Environment=INSECT_AGENT_ENABLED=1" "${UNIT_TMP}"
  fi

  run_as_root install -m 0644 "${UNIT_TMP}" /etc/systemd/system/insect-backend.service
  rm -f "${UNIT_TMP}"
  run_as_root systemctl daemon-reload
  run_as_root systemctl enable insect-backend

  # 清掉上一次可能残留的配对码，避免把过期的码当成这次的新码打印出来。
  rm -f "${PROJECT_DIR}/backend/data/agent_pairing.txt"

  if ! run_as_root systemctl restart insect-backend; then
    echo "!! systemctl restart 失败。最近的日志：" >&2
    run_as_root journalctl -u insect-backend -n 40 --no-pager >&2 || true
    exit 1
  fi

  echo "==> 等待健康检查"
  HEALTH_OK=0
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8000/api/health > /tmp/insect-health.json 2>/dev/null; then
      HEALTH_OK=1
      break
    fi
    sleep 1
  done

  if [[ "${HEALTH_OK}" -eq 1 ]]; then
    cat /tmp/insect-health.json
    echo
    if grep -Eq '"backend"[[:space:]]*:[[:space:]]*"onnx"' /tmp/insect-health.json; then
      echo "==> 推理后端是 onnx，正确"
    else
      echo "!! 推理后端不是 onnx，检查 onnxruntime 与模型路径（journalctl -u insect-backend）"
    fi
  else
    echo "!! 30 秒内没有通过健康检查。查看日志："
    echo "     sudo journalctl -u insect-backend -n 80 --no-pager"
  fi

  if [[ -n "${CENTER_URL}" ]]; then
    STATE_FILE="${PROJECT_DIR}/backend/data/agent.json"
    if [[ -f "${STATE_FILE}" ]] && \
       grep -Eq '"device_token"[[:space:]]*:[[:space:]]*"[^"]+"' "${STATE_FILE}"; then
      echo "==> 这台盒子已经配对过（agent.json 里有 device_token），不需要再输码。"
      echo "    要换账号：先在中心网页解绑，然后执行"
      echo "      rm ${STATE_FILE} && sudo systemctl restart insect-backend"
    else
      echo "==> 等待配对码（中心：${CENTER_URL}）"
      PAIR_CODE=""
      for _ in $(seq 1 20); do
        if [[ -s "${PROJECT_DIR}/backend/data/agent_pairing.txt" ]]; then
          PAIR_CODE="$(head -n 1 "${PROJECT_DIR}/backend/data/agent_pairing.txt")"
          break
        fi
        sleep 1
      done

      if [[ -n "${PAIR_CODE}" ]]; then
        cat <<EOF

  ============================================================
     设备配对码： ${PAIR_CODE}
     在**中心**网页操作：我的设备 → 绑定设备 → 输入它
     中心地址： ${CENTER_URL}
     有效期 5 分钟；过期后这里会自动打印新码
  ============================================================
EOF
      else
        echo "!! 还没拿到配对码。先确认盒子能连到中心："
        echo "     curl -fsS ${CENTER_URL}/api/health"
        echo "   然后看日志：sudo journalctl -u insect-backend -f"
      fi
    fi
  fi

  echo
  echo "==> 完成"
  echo "   查看状态：systemctl status insect-backend --no-pager"
  echo "   查看日志：sudo journalctl -u insect-backend -f"
  if [[ -n "${CENTER_URL}" ]]; then
    echo "   换了中心地址：编辑 /etc/systemd/system/insect-backend.service 里的"
    echo "     INSECT_AGENT_CENTER_URL，然后 sudo systemctl daemon-reload && sudo systemctl restart insect-backend"
  fi
  exit 0
fi

# ---------------------------------------------------------- 未装服务
echo
echo "==> 依赖已就绪（未安装 systemd 服务）"
cat <<NEXT
  手工启动（★ 必须进 backend/，\`app\` 包在那里）：
    cd ${PROJECT_DIR}/backend
NEXT

if [[ -n "${CENTER_URL}" ]]; then
  cat <<NEXT
    INSECT_AGENT_ENABLED=1 INSECT_AGENT_CENTER_URL=${CENTER_URL} \\
      ../.venv/bin/python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
NEXT
else
  cat <<'NEXT'
    ../.venv/bin/python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
NEXT
fi

cat <<NEXT

  另开终端验证：
    curl -s http://127.0.0.1:8000/api/health
  期望 "backend":"onnx" 且 "fallback_reason":null

  要装成开机自启（推荐）：
    bash ${PROJECT_DIR}/deploy/install-pi.sh${CENTER_URL:+ --center ${CENTER_URL}} --service
NEXT

if [[ -n "${CENTER_URL}" ]]; then
  cat <<NEXT

  配对码：启动后看终端日志，或 cat ${PROJECT_DIR}/backend/data/agent_pairing.txt
  绑定在**中心**网页完成：我的设备 → 绑定设备（不是盒子自己的网页）。
NEXT
fi

if [[ "${WITH_SYSTEM_SITE}" -eq 0 ]]; then
  cat <<NEXT

  插 CSI 排线摄像头时再补一步：
    sudo apt-get install -y python3-picamera2
    rm -rf ${VENV_DIR} && bash ${PROJECT_DIR}/deploy/install-pi.sh --csi
  只插 USB 摄像头则不需要。
NEXT
fi
