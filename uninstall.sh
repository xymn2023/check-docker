#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo '请以 root 运行'; exit 1; }
echo '卸载 Bot 服务默认保留配置、事务和版本目录，不删除任何 Docker 容器、镜像或数据卷。'
read -r -p '输入 UNINSTALL 确认: ' answer
[[ "$answer" == UNINSTALL ]] || exit 0
systemctl stop docker-update-bot.service || true
systemctl disable docker-update-bot.service || true
rm -f /etc/systemd/system/docker-update-bot.service
systemctl daemon-reload
echo '服务已卸载，/opt/docker-update-bot 保留。'
read -r -p '如需连同配置和事务文件一起删除，输入 DELETE-DATA（否则回车）: ' answer
if [[ "$answer" == DELETE-DATA ]]; then
  rm -rf -- /opt/docker-update-bot
  echo 'Bot 文件已删除；Docker 容器、镜像和卷未删除。'
fi
