#!/bin/bash

# 设置安装目录与 Git 仓库地址
INSTALL_DIR="/opt/docker-update-bot"
REPO_RAW_URL="https://raw.githubusercontent.com/xymn2023/check-docker/main"

echo "=========================================="
echo "   Docker Auto Update Bot 一键安装部署"
echo "=========================================="

# 1. 检查是否为 Root 权限
if [ "$EUID" -ne 0 ]; then
  echo "❌ 错误：请使用 root 权限运行此脚本！"
  exit 1
fi

# 2. 自动检测并安装系统基础依赖 (curl, python3, pip3)
echo "🔍 正在检查系统基础工具..."

INSTALL_CMD=""
if command -v apt-get &> /dev/null; then
    INSTALL_CMD="apt-get update -y && apt-get install -y"
elif command -v yum &> /dev/null; then
    INSTALL_CMD="yum install -y"
elif command -v dnf &> /dev/null; then
    INSTALL_CMD="dnf install -y"
fi

# 自动检测 curl
if ! command -v curl &> /dev/null; then
    echo "📦 未检测到 curl，正在自动安装..."
    $INSTALL_CMD curl
fi

# 自动检测 python3
if ! command -v python3 &> /dev/null; then
    echo "📦 未检测到 python3，正在自动安装..."
    $INSTALL_CMD python3
fi

# 自动检测 pip3
if ! command -v pip3 &> /dev/null; then
    echo "📦 未检测到 python3-pip，正在自动安装..."
    $INSTALL_CMD python3-pip || $INSTALL_CMD python3-pip
fi

# 3. 自动检测并安装 Python 库依赖 (已安装则自动跳过)
echo "🐍 正在检查 Python 模块依赖 (python-telegram-bot, requests)..."

# 检查 python-telegram-bot
if ! python3 -c "import telegram" &> /dev/null; then
    echo "📦 正在自动安装 python-telegram-bot..."
    pip3 install python-telegram-bot --break-system-packages 2>/dev/null || pip3 install python-telegram-bot
else
    echo "✅ Python 模块 python-telegram-bot 已存在，跳过安装。"
fi

# 检查 requests (解决 watchdog 报错关键)
if ! python3 -c "import requests" &> /dev/null; then
    echo "📦 正在自动安装 requests..."
    pip3 install requests --break-system-packages 2>/dev/null || pip3 install requests
else
    echo "✅ Python 模块 requests 已存在，跳过安装。"
fi

# 4. 创建安装目录并下载项目文件
echo "🚀 正在从 GitHub 拉取最新项目文件..."
mkdir -p ${INSTALL_DIR}

curl -fsSL ${REPO_RAW_URL}/autoupdate_bot.py -o ${INSTALL_DIR}/autoupdate_bot.py
curl -fsSL ${REPO_RAW_URL}/watchdog.py -o ${INSTALL_DIR}/watchdog.py
curl -fsSL ${REPO_RAW_URL}/uninstallsh.sh -o ${INSTALL_DIR}/uninstall.sh

chmod +x ${INSTALL_DIR}/uninstall.sh

# 5. 配置与交互
CONFIG_FILE="${INSTALL_DIR}/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo ""
    read -p "请输入你的 Telegram Bot Token: " BOT_TOKEN
    read -p "请输入你的 Telegram Chat ID: " CHAT_ID

    cat <<EOF > $CONFIG_FILE
{
  "bot_token": "$BOT_TOKEN",
  "chat_id": "$CHAT_ID"
}
EOF
    echo "✅ 配置文件 config.json 已创建。"
else
    echo "✅ 检测到已存在配置文件，跳过配置。"
fi

# 6. 配置 Systemd 开机自启服务
echo "⚙️ 正在配置 Systemd 系统服务..."

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

# 7. 启动服务并检查状态
systemctl daemon-reload
systemctl enable docker-update-bot.service
systemctl restart docker-update-bot.service

echo ""
echo "=========================================="
echo "🎉 安装完成！服务已成功启动！"
echo "=========================================="