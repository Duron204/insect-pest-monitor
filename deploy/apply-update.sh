#!/usr/bin/env bash
# ==========================================================
# 在树莓派上应用 update-pi.ps1 上传的增量目录。
# 一般不需要手工运行；由 deploy/update-pi.ps1 通过 ssh 调用。
#
#   bash /tmp/insect-apply-update.sh <项目目录> [--with-deps] [--no-restart]
#
# 做的事：
#   1. 检查增量目录（默认 /tmp/insect-update-payload）
#   2. 用"新目录 → 改名"的方式替换 backend/app（以及 vue-frontend/dist、
#      tools、backend/migrations，如果增量目录里有）
#   3. 可选：pip install -r backend/requirements-edge.txt
#   4. 重启 insect-backend 并等待 /api/health
#
# 为什么不是直接 cp 覆盖：覆盖不会删除本地已删除的文件，改名后的模块会
# 以 .pyc/旧模块的形式继续存在。先复制成 app.new、再 mv 换掉，能保证
# "树莓派上的 app/ 与开发机完全一致"。
#
# 这里**不需要 tar**：增量包以普通目录的形式传上来。
# ==========================================================
set -euo pipefail

PROJECT_DIR="${1:?用法: insect-apply-update.sh <项目目录> [--with-deps] [--no-restart]}"
shift

WITH_DEPS=0
NO_RESTART=0
for arg in "$@"; do
  case "${arg}" in
    --with-deps) WITH_DEPS=1 ;;
    --no-restart) NO_RESTART=1 ;;
    *)
      echo "未知参数：${arg}" >&2
      exit 2
      ;;
  esac
done

PAYLOAD="${INSECT_UPDATE_PAYLOAD:-/tmp/insect-update-payload}"

[[ -d "${PAYLOAD}" ]] || { echo "找不到增量目录：${PAYLOAD}" >&2; exit 1; }
[[ -d "${PROJECT_DIR}" ]] || { echo "项目目录不存在：${PROJECT_DIR}" >&2; exit 1; }

swap_dir() {
  local src="$1" dst="$2"
  [[ -d "${src}" ]] || return 0
  echo "==> 替换 ${dst}"
  rm -rf "${dst}.new" "${dst}.old"
  cp -a "${src}" "${dst}.new"
  if [[ -d "${dst}" ]]; then
    mv "${dst}" "${dst}.old"
  fi
  mv "${dst}.new" "${dst}"
  rm -rf "${dst}.old"
}

swap_dir "${PAYLOAD}/backend/app" "${PROJECT_DIR}/backend/app"
swap_dir "${PAYLOAD}/vue-frontend/dist" "${PROJECT_DIR}/vue-frontend/dist"
swap_dir "${PAYLOAD}/tools" "${PROJECT_DIR}/tools"
swap_dir "${PAYLOAD}/backend/migrations" "${PROJECT_DIR}/backend/migrations"

if [[ "${WITH_DEPS}" -eq 1 ]]; then
  echo "==> 更新 Python 依赖"
  "${PROJECT_DIR}/.venv/bin/python" -m pip install -r "${PROJECT_DIR}/backend/requirements-edge.txt"
fi

if [[ "${NO_RESTART}" -eq 1 ]]; then
  echo "==> 已跳过重启（--no-restart）"
  exit 0
fi

echo "==> 重启 insect-backend"
# 有免密 sudo 就静默重启；没有则让 sudo 正常提示密码（update-pi.ps1 用 ssh -t）。
if ! sudo -n systemctl restart insect-backend 2>/dev/null; then
  if ! sudo systemctl restart insect-backend; then
    echo "!! 重启失败。请手动执行：sudo systemctl restart insect-backend" >&2
    exit 3
  fi
fi

echo "==> 等待健康检查"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8000/api/health > /tmp/insect-health.json 2>/dev/null; then
    cat /tmp/insect-health.json
    echo
    exit 0
  fi
  sleep 1
done

echo "!! 30 秒内健康检查未通过：sudo journalctl -u insect-backend -n 80 --no-pager" >&2
exit 4
