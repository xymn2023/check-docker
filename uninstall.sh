#!/usr/bin/env bash
set -e

INSTALL_DIR="/opt/docker-update-bot"
SERVICE_FILE="/etc/systemd/system/docker-update-bot.service"

echo "⚠️  警告：此操作将彻底停止服务，并清空所有已保存的 Token、ChatID 以及镜像任务池！"
read -p "确认卸载？(y/N): " confirm

if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "操作已取消。"
    exit 0
fi

echo "🛑 正在停止并禁用 Systemd 服务..."
systemctl stop docker-update-bot.service || true
systemctl disable docker-update-bot.service || true

if [ -f "${SERVICE_FILE}" ]; then
    rm -f ${SERVICE_FILE}
    systemctl daemon-reload
fi

echo "🗑️ 正在清空并删除保存文件与程序目录: ${INSTALL_DIR} ..."
rm -rf ${INSTALL_DIR}

echo "========================================="
echo "✅ 卸载完成！所有保存的配置文件和任务已被彻底清理。"
echo "========================================="