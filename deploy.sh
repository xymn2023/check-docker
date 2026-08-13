#!/bin/bash

# 设置安装目录与 Git 仓库地址
INSTALL_DIR="/opt/docker-update-bot"
REPO_RAW_URL="https://raw.githubusercontent.com/xymn2023/check-docker/main"
CONFIG_FILE="${INSTALL_DIR}/config.json"

# 1. 检查是否为 Root 权限
if [ "$EUID" -ne 0 ]; then
  echo "❌ 错误：请使用 root 权限运行此脚本！"
  exit 1
fi

# ==============================================================================
# 🎛️ 管理界面 (检测到已安装时直接触发，无需重新安装/配置)
# ==============================================================================
show_menu() {
    while true; do
        clear
        echo "=========================================="
        echo "   🤖 Docker Auto Update Bot 管理面板"
        echo "=========================================="
        echo " 状态: $(systemctl is-active docker-update-bot.service 2>/dev/null | grep -q 'active' && echo '🟢 正在运行' || echo '🔴 已停止')"
        echo "------------------------------------------"
        echo " 1. 查看 服务运行日志 (journalctl)"
        echo " 2. 重启 Bot 服务"
        echo " 3. 停止 Bot 服务"
        echo " 4. 启动 Bot 服务"
        echo " 5. 修改配置 (Bot Token / Chat ID)"
        echo " 6. 强制覆盖重装 / 重新下载源码"
        echo " 7. 卸载本程序"
        echo " 0. 退出管理界面"
        echo "=========================================="
        read -p "请输入选项 [0-7]: " choice

        case $choice in
            1)
                echo "📄 正在调取服务运行日志 (按 Ctrl+C 退出查看)..."
                journalctl -u docker-update-bot.service -n 50 -f
                ;;
            2)
                systemctl restart docker-update-bot.service
                echo "✅ 服务已重启！"
                sleep 2
                ;;
            3)
                systemctl stop docker-update-bot.service
                echo "🛑 服务已停止！"
                sleep 2
                ;;
            4)
                systemctl start docker-update-bot.service
                echo "🚀 服务已启动！"
                sleep 2
                ;;
            5)
                echo "📝 重新配置 Telegram 信息："
                read -p "请输入新的 Telegram Bot Token: " NEW_TOKEN
                read -p "请输入新的 Telegram Chat ID: " NEW_CHAT_ID
                if [ -n "$NEW_TOKEN" ] && [ -n "$NEW_CHAT_ID" ]; then
                    cat <<EOF > $CONFIG_FILE
{
  "bot_token": "$NEW_TOKEN",
  "chat_id": "$NEW_CHAT_ID"
}
EOF
                    systemctl restart docker-update-bot.service
                    echo "✅ 配置已更新并重启服务！"
                else
                    echo "⚠️ 输入有误，未修改配置。"
                fi
                sleep 2
                ;;
            6)
                read -p "❓ 确定要强制覆盖重装吗？(y/N): " confirm
                if [[ "$confirm" =~ ^[Yy]$ ]]; then
                    install_process
                    return
                fi
                ;;
            7)
                if [ -f "${INSTALL_DIR}/uninstall.sh" ]; then
                    bash ${INSTALL_DIR}/uninstall.sh
                else
                    echo "❌ 未找到卸载脚本，请检查目录！"
                fi
                exit 0
                ;;
            0)
                echo "👋 已退出。"
                exit 0
                ;;
            *)
                echo "❌ 无效选项，请重新输入！"
                sleep 1
                ;;
        esac
    done
}

# ==============================================================================
# 📦 安装流程 (全新安装或选择重装时执行)
# ==============================================================================
install_process() {
    echo "🔍 正在检查并安装系统基础工具 (curl, python3, pip3)..."

    INSTALL_CMD=""
    if command -v apt-get &> /dev/null; then
        INSTALL_CMD="apt-get update -y && apt-get install -y"
    elif command -v yum &> /dev/null; then
        INSTALL_CMD="yum install -y"
    elif command -v dnf &> /dev/null; then
        INSTALL_CMD="dnf install -y"
    fi

    if ! command -v curl &> /dev/null; then $INSTALL_CMD curl; fi
    if ! command -v python3 &> /dev/null; then $INSTALL_CMD python3; fi
    if ! command -v pip3 &> /dev/null; then $INSTALL_CMD python3-pip || $INSTALL_CMD python3-pip; fi

    echo "🐍 自动检测并补齐 Python 依赖库..."
    if ! python3 -c "import telegram" &> /dev/null; then
        pip3 install python-telegram-bot --break-system-packages 2>/dev/null || pip3 install python-telegram-bot
    fi

    if ! python3 -c "import requests" &> /dev/null; then
        pip3 install requests --break-system-packages 2>/dev/null || pip3 install requests
    fi

    echo "🚀 正在从 GitHub 下载项目最新代码..."
    mkdir -p ${INSTALL_DIR}

    curl -fsSL ${REPO_RAW_URL}/autoupdate_bot.py -o ${INSTALL_DIR}/autoupdate_bot.py
    curl -fsSL ${REPO_RAW_URL}/watchdog.py -o ${INSTALL_DIR}/watchdog.py
    curl -fsSL ${REPO_RAW_URL}/uninstallsh.sh -o ${INSTALL_DIR}/uninstall.sh
    chmod +x ${INSTALL_DIR}/uninstall.sh

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
    fi

    echo "⚙️ 配置 Systemd 服务..."
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

    echo "🎉 安装并启动完成！即将进入管理界面..."
    sleep 2
    show_menu
}

# ==============================================================================
# 🚀 脚本入口判重逻辑
# ==============================================================================
# 只要检测到服务已存在或配置文件已生成，说明已安装，直接弹管理菜单！
if [ -f "/etc/systemd/system/docker-update-bot.service" ] || [ -f "$CONFIG_FILE" ]; then
    echo "💡 检测到本程序已经安装，自动直接进入管理界面..."
    sleep 1
    show_menu
else
    # 全新机器直接走自动检测安装流程
    install_process
fi