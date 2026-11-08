#!/usr/bin/env bash
# ==========================================================
# 现场 7×24 巡检：把关键指标按周期追加到 CSV，用于事后分析而不是"凭感觉"。
#
# 安装（配合 timer 每 10 分钟执行一次）：
#   sudo mkdir -p /var/log/insect
#   sudo cp deploy/field-soak.sh /usr/local/bin/ && sudo chmod +x /usr/local/bin/field-soak.sh
#   sudo cp deploy/field-soak.service deploy/field-soak.timer /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now field-soak.timer
#
# 可选：开启"带鉴权的推理探针"，才能真正跟踪推理耗时是否漂移：
#   export INSECT_PROBE_USER=pi INSECT_PROBE_PASS=... INSECT_PROBE_IMAGE=/opt/insect/probe.jpg
# ==========================================================
set -uo pipefail

OUT="${INSECT_SOAK_CSV:-/var/log/insect/soak.csv}"
API_BASE="${INSECT_API_BASE:-http://127.0.0.1:8000}"
HEALTH="${INSECT_HEALTH_URL:-${API_BASE}/api/health}"
SERVICE="${INSECT_SERVICE_NAME:-insect-backend}"
PROBE_IMAGE="${INSECT_PROBE_IMAGE:-}"
PROBE_USER="${INSECT_PROBE_USER:-}"
PROBE_PASS="${INSECT_PROBE_PASS:-}"

mkdir -p "$(dirname "${OUT}")"
if [[ ! -s "${OUT}" ]]; then
  # 列名与后端字段对齐：total_tasks 来自 /api/stats/summary 的同名字段。
  # 空值表示"没测到"（例如没给凭据、或本机没有 vcgencmd），**不代表 0**。
  echo "utc_time,health_ok,backend,model_loaded,fallback,health_ms,cpu_temp_c,throttled,disk_free_mb,mem_free_mb,restarts,probe_ms,total_tasks" > "${OUT}"
fi

now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---- 健康接口（同时量一个粗略的响应耗时）----
start_ms="$(date +%s%3N)"
body="$(curl -fsS --max-time 15 "${HEALTH}" 2>/dev/null)"
end_ms="$(date +%s%3N)"
health_ms=$(( end_ms - start_ms ))

health_ok=0
backend=""
model_loaded=""
fallback=""
task_count=""
if [[ -n "${body}" ]]; then
  health_ok=1
  backend="$(printf '%s' "${body}" | sed -n 's/.*"backend":"\([^"]*\)".*/\1/p')"
  model_loaded="$(printf '%s' "${body}" | sed -n 's/.*"model_loaded":\([a-z]*\).*/\1/p')"
  # fallback_reason 为 null 表示没有回退；非 null 时原样记录（含引号内的原因）
  fallback="$(printf '%s' "${body}" | sed -n 's/.*"fallback_reason":\(null\|"[^"]*"\).*/\1/p')"
fi

# ---- 树莓派专属指标（非 Pi 上命令不存在，置空即可）----
cpu_temp=""
if command -v vcgencmd >/dev/null 2>&1; then
  cpu_temp="$(vcgencmd measure_temp 2>/dev/null | sed -n "s/temp=\([0-9.]*\).*/\1/p")"
fi
throttled=""
if command -v vcgencmd >/dev/null 2>&1; then
  throttled="$(vcgencmd get_throttled 2>/dev/null | sed -n 's/throttled=//p')"
fi

disk_free_mb="$(df -Pm / 2>/dev/null | awk 'NR==2 {print $4}')"
mem_free_mb="$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null)"
restarts="$(systemctl show -p NRestarts --value "${SERVICE}" 2>/dev/null)"

# ---- 需要鉴权的两项：总任务数 + 真实推理耗时 ----
# 只登录一次供两者复用；没给凭据时这两列为空（"没测"而不是 0）。
probe_ms=""
total_tasks=""
token=""
if [[ -n "${PROBE_USER}" && -n "${PROBE_PASS}" ]]; then
  token="$(curl -fsS --max-time 15 -X POST "${API_BASE}/api/auth/login" \
      -H 'Content-Type: application/json' \
      -d "{\"username\":\"${PROBE_USER}\",\"password\":\"${PROBE_PASS}\"}" 2>/dev/null \
      | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')"
fi

if [[ -n "${token}" ]]; then
  summary="$(curl -fsS --max-time 20 "${API_BASE}/api/stats/summary" \
      -H "Authorization: Bearer ${token}" 2>/dev/null)"
  total_tasks="$(printf '%s' "${summary}" | sed -n 's/.*"total_tasks":\([0-9]*\).*/\1/p')"

  # 推理探针：用一张固定样图反复检测，才能看出推理耗时是否随时间漂移
  if [[ -n "${PROBE_IMAGE}" && -f "${PROBE_IMAGE}" ]]; then
    probe_body="$(curl -fsS --max-time 60 -X POST "${API_BASE}/api/detection/image" \
        -H "Authorization: Bearer ${token}" \
        -F "file=@${PROBE_IMAGE}" 2>/dev/null)"
    probe_ms="$(printf '%s' "${probe_body}" | sed -n 's/.*"inference_ms":\([0-9.]*\).*/\1/p')"
  fi
fi

echo "${now},${health_ok},${backend},${model_loaded},${fallback},${health_ms},${cpu_temp},${throttled},${disk_free_mb},${mem_free_mb},${restarts},${probe_ms},${total_tasks}" >> "${OUT}"

# 异常时在 journal 里留一条明显的记录，便于事后按时间点定位
if [[ "${health_ok}" -eq 0 ]]; then
  echo "健康检查失败：${HEALTH}"
fi
if [[ -n "${fallback}" && "${fallback}" != "null" ]]; then
  echo "警告：后端处于回退状态 ${fallback}"
fi
if [[ -n "${throttled}" && "${throttled}" != "0x0" ]]; then
  echo "警告：发生过降频/欠压 throttled=${throttled}（先解决供电与散热，性能数据才有意义）"
fi
exit 0
