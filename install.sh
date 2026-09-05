#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/docker-update-bot"
SERVICE_FILE="/etc/systemd/system/docker-update-bot.service"
SERVICE="docker-update-bot.service"

require_root() {
  [[ $EUID -eq 0 ]] || { echo '请使用 sudo bash deploy.sh'; exit 1; }
}

install_release() {
  for command in python3 docker systemctl; do
    command -v "$command" >/dev/null || { echo "缺少依赖：$command；请参阅 docs/DEPLOY.md"; return 1; }
  done
  python3 -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10+"'
  docker info >/dev/null
  docker compose version
  mkdir -p "$INSTALL_DIR/releases"
  chmod 700 "$INSTALL_DIR" "$INSTALL_DIR/releases"
  local release previous old_service
  release="$(mktemp -d "$INSTALL_DIR/releases/v2.0.0-XXXXXXXX")"
  cp "$SOURCE_DIR/"{autoupdate_bot.py,core.py,admin.py,requirements.txt,requirements.lock,uninstall.sh} "$release/"
  if [[ -f "$SOURCE_DIR/.source-commit" ]]; then cp "$SOURCE_DIR/.source-commit" "$release/"; fi
  python3 -m venv "$release/venv"
  "$release/venv/bin/python" -m pip install -r "$release/requirements.lock"
  "$release/venv/bin/python" -c "import sys; sys.path.insert(0, '$release'); import core, autoupdate_bot"
  "$release/venv/bin/python" "$release/admin.py" --data-dir "$INSTALL_DIR"
  previous="$(readlink "$INSTALL_DIR/current" || true)"
  old_service="$release/previous.service"
  if [[ -f "$SERVICE_FILE" ]]; then cp "$SERVICE_FILE" "$old_service"; fi
  rollback_activation() {
    trap - ERR
    systemctl stop "$SERVICE" || true
    if [[ -n "$previous" ]]; then
      ln -sfn "$previous" "$INSTALL_DIR/current.restore"
      mv -Tf "$INSTALL_DIR/current.restore" "$INSTALL_DIR/current"
    fi
    if [[ -f "$old_service" ]]; then
      cp "$old_service" "$SERVICE_FILE"
      systemctl daemon-reload
      systemctl reset-failed "$SERVICE" || true
      systemctl start "$SERVICE" || true
      echo '已请求启动旧服务，请使用 systemctl status 检查。'
    else
      systemctl disable "$SERVICE" || true
      echo '首次安装未就绪；文件保留，修复配置后重新安装。'
    fi
  }
  if systemctl cat "$SERVICE" >/dev/null 2>&1; then systemctl stop "$SERVICE"; fi
  trap 'rollback_activation' ERR
  ln -s "$release" "$INSTALL_DIR/current.next"
  mv -Tf "$INSTALL_DIR/current.next" "$INSTALL_DIR/current"
  cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=check-docker v2 Telegram update service
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=/opt/docker-update-bot
ExecStart=/opt/docker-update-bot/current/venv/bin/python /opt/docker-update-bot/current/autoupdate_bot.py
Environment=PYTHONUNBUFFERED=1
Environment=CHECK_DOCKER_DATA_DIR=/opt/docker-update-bot
Restart=on-failure
RestartSec=10
TimeoutStopSec=45
KillMode=control-group
UMask=0077
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl reset-failed "$SERVICE" || true
  rm -f "$INSTALL_DIR/ready.json"
  systemctl enable "$SERVICE"
  systemctl start "$SERVICE"
  echo '等待 Bot 建立连接（最多 60 秒）…'
  local ok=0
  for ((attempt=0; attempt<60; attempt++)); do
    if [[ -f "$INSTALL_DIR/ready.json" ]] && systemctl is-active --quiet "$SERVICE"; then ok=1; break; fi
    sleep 1
  done
  if [[ "$ok" == 1 ]]; then
    trap - ERR
    echo "安装完成：$release"
    echo '发送 /scan 管理任务；发送 /help 查看命令。'
  else
    echo '新版本未就绪，正在恢复原服务配置。请检查 Telegram 网络和 Token。'
    rollback_activation
    return 1
  fi
}

require_root
if [[ "${1:-}" == '--install' || ! -f "$SERVICE_FILE" || ! -f "$INSTALL_DIR/config.json" ]]; then install_release; exit; fi
while true; do
  echo 'check-docker v2 管理菜单'
  echo '1. 安装/升级本次获取的版本'
  echo '2. 查看状态'
  echo '3. 查看最近日志'
  echo '4. 验证配置并重启'
  echo '5. 停止服务'
  echo '6. 卸载'
  echo '0. 退出'
  read -r -p '选择: ' choice
  case "$choice" in
    1) install_release ;;
    2) systemctl status "$SERVICE" --no-pager || true ;;
    3) journalctl -u "$SERVICE" -n 80 --no-pager ;;
    4) "$INSTALL_DIR/current/venv/bin/python" "$INSTALL_DIR/current/admin.py" --validate
       systemctl reset-failed "$SERVICE" || true
       systemctl restart "$SERVICE" ;;
    5) systemctl stop "$SERVICE" ;;
    6) bash "$SOURCE_DIR/uninstall.sh" ;;
    0) clear; exit 0 ;;
    *) echo '输入无效' ;;
  esac
  read -r -p '回车返回菜单…' unused
 done
