# ==========================================================
# 运行期维护：清理过期产物 + 在线备份数据库
#
#   sudo cp deploy/insect-maintenance.sh /usr/local/bin/
#   sudo chmod +x /usr/local/bin/insect-maintenance.sh
#   sudo cp deploy/insect-maintenance.service deploy/insect-maintenance.timer /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now insect-maintenance.timer
#
# 手工跑一次看效果：
#   sudo systemctl start insect-maintenance.service
#   journalctl -u insect-maintenance -n 40 --no-pager
#
# 为什么需要它：
#   * 现场 7×24 跑下去 outputs/ 只增不减 —— SD 卡迟早满（抓拍每天约数 MB，
#     视频任务更快）；保留天数由 config.yaml 的 storage.retention_days 控制。
#   * 数据库是唯一一份账号/历史/告警，卡坏或误删就全丢；
#     备份用 SQLite 在线备份 API，不需要停服务。
#
# 注意：备份文件与数据库在**同一张 SD 卡**上，防的是"误删/写坏"而不是"卡坏"。
#       要防卡坏，请定期把 backend/data/backups/ 同步到 U 盘或另一台机器。
# ==========================================================
set -euo pipefail

PROJECT_DIR="${INSECT_PROJECT_DIR:-/opt/insect/InsectPestDetection}"
PY="${PROJECT_DIR}/.venv/bin/python"

if [[ ! -x "${PY}" ]]; then
  echo "!! 找不到解释器 ${PY}（先跑 deploy/install-pi.sh）"
  exit 1
fi

cd "${PROJECT_DIR}"

echo "==> 1/2 清理过期产物与上传文件"
"${PY}" tools/purge_artifacts.py

echo
echo "==> 2/2 在线备份数据库"
"${PY}" tools/backup_db.py

echo
echo "==> 磁盘占用"
du -sh "${PROJECT_DIR}/outputs" 2>/dev/null || true
du -sh "${PROJECT_DIR}/backend/data" 2>/dev/null || true
du -sh "${PROJECT_DIR}/backend/data/backups" 2>/dev/null || true
