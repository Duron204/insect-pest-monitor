#!/usr/bin/env bash
# ==========================================================
# 后端看门狗：定期探测 /api/health，连续失败达到阈值才重启服务。
#
# 为什么不用 systemd 的 WatchdogSec：
#   uvicorn 不实现 sd_notify，不会发送 WATCHDOG=1，
#   照抄 WatchdogSec 会让 systemd 把健康的进程反复杀掉。
#
# 为什么要"连续 N 次失败"才动手：
#   单次超时可能只是推理正忙（一张大图 + 4 核 CPU），
#   立刻重启反而会把正在跑的任务打断。
# ==========================================================
set -uo pipefail

URL="${INSECT_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
SERVICE="${INSECT_SERVICE_NAME:-insect-backend}"
STATE_FILE="${INSECT_WATCHDOG_STATE:-/run/insect-watchdog.failures}"
THRESHOLD="${INSECT_WATCHDOG_THRESHOLD:-3}"
TIMEOUT="${INSECT_WATCHDOG_TIMEOUT:-20}"

failures=0
if [[ -r "${STATE_FILE}" ]]; then
  failures="$(cat "${STATE_FILE}" 2>/dev/null || echo 0)"
  [[ "${failures}" =~ ^[0-9]+$ ]] || failures=0
fi

body="$(curl -fsS --max-time "${TIMEOUT}" "${URL}" 2>/dev/null)"
healthy=0
# 不仅要求 HTTP 200，还要求真的是本服务在应答（避免被别的程序占用端口而误判健康）
if [[ -n "${body}" && "${body}" == *'"backend"'* && "${body}" == *'"status":"ok"'* ]]; then
  healthy=1
fi

if [[ "${healthy}" -eq 1 ]]; then
  if [[ "${failures}" -ne 0 ]]; then
    echo "已恢复健康（此前连续失败 ${failures} 次）"
  fi
  echo 0 > "${STATE_FILE}"
  # 额外记录：后端是否发生了回退（配了 onnx 却跑在 torch 上属于异常态）
  if [[ "${body}" == *'"fallback_reason":null'* ]]; then
    echo "后端回退检查：正常（fallback_reason 为空）"
  else
    echo "警告：后端发生了回退 -> ${body}"
  fi
  exit 0
fi

failures=$((failures + 1))
echo "${failures}" > "${STATE_FILE}"
echo "健康检查失败（连续第 ${failures} 次，阈值 ${THRESHOLD}）：${URL}"

if [[ "${failures}" -ge "${THRESHOLD}" ]]; then
  echo "达到阈值，重启 ${SERVICE}"
  systemctl restart "${SERVICE}"
  echo 0 > "${STATE_FILE}"
fi
exit 0
