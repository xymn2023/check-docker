#!/usr/bin/env bash
set -e

INSTALL_DIR="/opt/docker-update-bot"
CONFIG_FILE="${INSTALL_DIR}/config.json"
REPO_RAW_URL="https://raw.githubusercontent.com/xymn2023/check-docker/main"

echo "========================================="
echo "   Docker 镜像自动更新 Bot 安装程序"
echo "========================================="

mkdir -p ${INSTALL_DIR}

# 1. 检查配置 JSON 文件是否存在
if [ -f "${CONFIG_FILE}" ]; then
    echo "🟢 检测到已存在的 config.json 配置文件！跳过手动输入阶段..."
else
    echo "⚠️ 未找到配置文件，请输入 Telegram Bot 配置信息："
    read -p "👉 请输入 TELEGRAM_BOT_TOKEN: " input_token
    read -p "👉 请输入 ALLOWED_CHAT_ID: " input_chat_id

    if [ -z "$input_token" ] || [ -z "$input_chat_id" ]; then
        echo "❌ Token 或 Chat ID 不能为空，安装中断。"
        exit 1
    fi

    cat <<EOF > ${CONFIG_FILE}
{
  "bot_token": "${input_token}",
  "chat_id": "${input_chat_id}"
}
EOF
    echo "✅ 配置文件已创建保存至: ${CONFIG_FILE}"
fi

# 2. 拉取最新代码
echo "📥 正在拉取程序文件..."
curl -fsSL ${REPO_RAW_URL}/autoupdate_bot.py -o ${INSTALL_DIR}/autoupdate_bot.py
curl -fsSL ${REPO_RAW_URL}/watchdog.py -o ${INSTALL_DIR}/watchdog.py

# 3. 部署 Systemd 服务
cat <<EOF > /etc/systemd/system/docker-update-bot.service
[Unit]
Description=Docker Auto Update Telegram Bot
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/watchdog.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable docker-update-bot.service
systemctl restart docker-update-bot.service

echo "========================================="
echo "🎉 安装/更新完成！服务已在后台启动。"
echo "========================================="