#!/bin/bash

MARKER_FILE="/opt/docker-update-bot/.need_restart"
SERVICE_NAME="docker-update-bot.service"

# 1. 优先检查是否有 Python 发出的重启申请
if [ -f "$MARKER_FILE" ]; then
    rm -f "$MARKER_FILE"
    echo "$(date): 检测到升级重启标记，正在触发 Systemd 重启..." >> /opt/docker-update-bot/watchdog.log
    systemctl restart "$SERVICE_NAME"
    exit 0
fi

# 2. 正常保活逻辑：如果服务挂了，直接拉起
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "$(date): 服务异常挂起，正在自动拉起..." >> /opt/docker-update-bot/watchdog.log
    systemctl start "$SERVICE_NAME"
fi