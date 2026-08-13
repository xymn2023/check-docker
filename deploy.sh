#!/usr/bin/env bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
PLAIN='\033[0m'

INSTALL_DIR="/opt/docker-update-bot"
CONFIG_FILE="${INSTALL_DIR}/config.json"
SERVICE_FILE="/etc/systemd/system/docker-update-bot.service"
REPO_RAW_URL="https://raw.githubusercontent.com/xymn2023/check-docker/main"

if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}错误：必须使用 root 权限运行此脚本！${PLAIN}"
   exit 1
fi

get_status() {
    if systemctl is-active --quiet docker-update-bot.service 2>/dev/null; then
        echo -e "${GREEN}正在运行${PLAIN}"
    elif systemctl is-enabled --quiet docker-update-bot.service 2>/dev/null; then
        echo -e "${YELLOW}已停止${PLAIN}"
    else
        echo -e "${RED}未安装${PLAIN}"
    fi
}

do_fetch_code() {
    echo -e "${BLUE}📥 正在从 GitHub 拉取最新程序文件...${PLAIN}"
    curl -fsSL ${REPO_RAW_URL}/autoupdate_bot.py -o ${INSTALL_DIR}/autoupdate_bot.py
    curl -fsSL ${REPO_RAW_URL}/watchdog.py -o ${INSTALL_DIR}/watchdog.py
}

do_deploy_service() {
    cat <<EOF > ${SERVICE_FILE}
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
}

install_bot() {
    echo -e "\n${GREEN}========================================="
    echo "    开始初始化安装 Docker 监控 Bot"
    echo -e "=========================================${PLAIN}\n"

    mkdir -p ${INSTALL_DIR}

    read -p "👉 请输入 TELEGRAM_BOT_TOKEN: " input_token
    read -p "👉 请输入 ALLOWED_CHAT_ID: " input_chat_id

    if [ -z "$input_token" ] || [ -z "$input_chat_id" ]; then
        echo -e "${RED}❌ Token 或 Chat ID 不能为空，安装取消。${PLAIN}"
        exit 1
    fi

    cat <<EOF > ${CONFIG_FILE}
{
  "bot_token": "${input_token}",
  "chat_id": "${input_chat_id}"
}
EOF
    echo -e "${GREEN}✅ 配置已写入: ${CONFIG_FILE}${PLAIN}"

    do_fetch_code
    do_deploy_service

    echo -e "\n${GREEN}🎉 安装完成！服务已在后台成功启动。${PLAIN}"
}

update_code() {
    echo -e "\n${BLUE}正在更新 Bot 程序源码...${PLAIN}"
    do_fetch_code
    systemctl restart docker-update-bot.service
    echo -e "${GREEN}✅ 程序更新成功，服务已重启！${PLAIN}"
}

reconfig() {
    echo -e "\n${YELLOW}修改 Telegram Bot 配置${PLAIN}"
    read -p "👉 请输入全新的 TELEGRAM_BOT_TOKEN: " input_token
    read -p "👉 请输入全新的 ALLOWED_CHAT_ID: " input_chat_id

    if [ -z "$input_token" ] || [ -z "$input_chat_id" ]; then
        echo -e "${RED}❌ 输入不能为空，取消修改。${PLAIN}"
        return
    fi

    cat <<EOF > ${CONFIG_FILE}
{
  "bot_token": "${input_token}",
  "chat_id": "${input_chat_id}"
}
EOF
    systemctl restart docker-update-bot.service
    echo -e "${GREEN}✅ 配置修改成功，服务已重启生效！${PLAIN}"
}

show_logs() {
    echo -e "${BLUE}正在调取服务运行日志 (按 Ctrl+C 退出查看)...${PLAIN}\n"
    journalctl -u docker-update-bot.service -n 50 -f
}

uninstall_bot() {
    echo -e "\n${RED}⚠️ 警告：卸载将完全擦除所有程序文件、Token 配置以及保存的镜像任务！${PLAIN}"
    read -p "确定要彻底卸载吗？(y/N): " confirm
    if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
        systemctl stop docker-update-bot.service || true
        systemctl disable docker-update-bot.service || true
        rm -f ${SERVICE_FILE}
        systemctl daemon-reload
        rm -rf ${INSTALL_DIR}
        echo -e "${GREEN}✅ 服务与配置文件已彻底清理卸载。${PLAIN}"
        exit 0
    else
        echo "已取消卸载。"
    fi
}

show_menu() {
    clear
    echo -e "${GREEN}========================================="
    echo "    Docker 镜像自动更新 Bot 管理面板"
    echo -e "=========================================${PLAIN}"
    echo -e "服务状态: $(get_status)"
    echo -e "安装路径: ${INSTALL_DIR}"
    echo "-----------------------------------------"
    echo " 1. 查看运行状态 / 日志"
    echo " 2. 更新 Bot 程序代码"
    echo " 3. 修改 Token / Chat ID 配置"
    echo " 4. 重启 Bot 服务"
    echo " 5. 停止 Bot 服务"
    echo " 6. 彻底卸载程序 (清除所有文件)"
    echo " 0. 退出菜单"
    echo "-----------------------------------------"
    read -p "请输入数字选择 [0-6]: " choice

    case "$choice" in
        1) show_logs ;;
        2) update_code ;;
        3) reconfig ;;
        4) 
            systemctl restart docker-update-bot.service
            echo -e "${GREEN}✅ 服务重启成功！${PLAIN}"
            ;;
        5) 
            systemctl stop docker-update-bot.service
            echo -e "${YELLOW}⏸️ 服务已停止。${PLAIN}"
            ;;
        6) uninstall_bot ;;
        0) exit 0 ;;
        *) echo -e "${RED}输入错误，请输入有效数字！${PLAIN}" ;;
    esac
}

if [ ! -f "${CONFIG_FILE}" ] || [ ! -f "${SERVICE_FILE}" ]; then
    install_bot
else
    show_menu
fi