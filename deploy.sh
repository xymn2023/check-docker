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
VENV_DIR="${INSTALL_DIR}/venv"
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

# -------------------- Python 虚拟环境与依赖 --------------------

install_python_deps() {
    echo -e "${BLUE}📦 正在检查并安装 Python 运行环境与依赖...${PLAIN}"

    # 确保 python3 和 pip3 可用
    if ! command -v python3 &> /dev/null; then
        echo -e "${YELLOW}正在安装 python3...${PLAIN}"
        if command -v apt-get &> /dev/null; then
            apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv
        elif command -v yum &> /dev/null; then
            yum install -y python3 python3-pip
        elif command -v dnf &> /dev/null; then
            dnf install -y python3 python3-pip
        else
            echo -e "${RED}❌ 无法自动安装 python3，请手动安装后重试。${PLAIN}"
            exit 1
        fi
    fi

    # 确保 venv 模块可用
    if ! python3 -c "import venv" &> /dev/null; then
        echo -e "${YELLOW}正在安装 python3-venv...${PLAIN}"
        if command -v apt-get &> /dev/null; then
            apt-get install -y -qq python3-venv
        fi
    fi

    # 确保 pip3 可用
    if ! command -v pip3 &> /dev/null; then
        echo -e "${YELLOW}正在安装 pip3...${PLAIN}"
        if command -v apt-get &> /dev/null; then
            apt-get install -y -qq python3-pip
        fi
    fi

    # 创建虚拟环境
    if [ ! -d "${VENV_DIR}" ]; then
        echo -e "${BLUE}🔧 正在创建 Python 虚拟环境...${PLAIN}"
        python3 -m venv "${VENV_DIR}"
    fi

    # 安装依赖（不再需要 [job-queue]，定时任务已改用 asyncio 原生实现）
    echo -e "${BLUE}📥 正在安装 python-telegram-bot 和 requests...${PLAIN}"
    "${VENV_DIR}/bin/pip" install --upgrade pip -q
    "${VENV_DIR}/bin/pip" install "python-telegram-bot>=20.0" "requests>=2.28.0" -q

    echo -e "${GREEN}✅ Python 依赖安装完成。${PLAIN}"
}

# -------------------- 代码拉取与服务部署 --------------------

do_fetch_code() {
    echo -e "${BLUE}📥 正在从 GitHub 拉取最新程序文件...${PLAIN}"
    curl -fsSL ${REPO_RAW_URL}/autoupdate_bot.py -o ${INSTALL_DIR}/autoupdate_bot.py
    curl -fsSL ${REPO_RAW_URL}/watchdog.py -o ${INSTALL_DIR}/watchdog.py
}

do_deploy_service() {
    # 自动选择 Python 解释器：优先使用 venv，回退到系统 python3
    local PYTHON_BIN="${VENV_DIR}/bin/python"
    if [ ! -x "${PYTHON_BIN}" ]; then
        PYTHON_BIN="/usr/bin/python3"
        echo -e "${YELLOW}⚠️ 未找到虚拟环境 Python，回退到系统 Python: ${PYTHON_BIN}${PLAIN}"
    else
        echo -e "${BLUE}🐍 使用虚拟环境 Python: ${PYTHON_BIN}${PLAIN}"
    fi

    cat <<EOF > ${SERVICE_FILE}
[Unit]
Description=Docker Auto Update Telegram Bot
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON_BIN} ${INSTALL_DIR}/watchdog.py
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

# -------------------- 安装流程 --------------------

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

    # 先安装 Python 依赖（venv），再拉代码和部署服务
    install_python_deps
    do_fetch_code
    do_deploy_service

    echo -e "\n${GREEN}🎉 安装完成！服务已在后台成功启动。${PLAIN}"
    echo -e "${GREEN}💡 发送 /status 到 Bot 检查运行状态。${PLAIN}"
    echo -e "${GREEN}💡 发送 /scan 扫描并选择要监控的镜像。${PLAIN}\n"
}

# -------------------- 更新代码（同时重建 service 文件和依赖） --------------------

update_code() {
    echo -e "\n${BLUE}正在更新 Bot 程序源码并重建服务配置...${PLAIN}"

    # 1. 确保虚拟环境和依赖是最新的
    install_python_deps

    # 2. 拉取最新代码
    do_fetch_code

    # 3. 重新生成 systemd service 文件（关键：确保 ExecStart 指向 venv Python）
    do_deploy_service

    echo -e "${GREEN}✅ 程序更新成功，服务已重启！${PLAIN}"
    echo -e "${GREEN}💡 请在 Telegram 中查看启动消息，确认「自动巡检」显示为 🟢 已启用。${PLAIN}"
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

# -------------------- 修复功能（解决旧版安装的 service 文件指向系统 Python 的问题） --------------------

repair_installation() {
    echo -e "\n${YELLOW}🔧 正在修复安装（重建虚拟环境、依赖和服务配置）...${PLAIN}"

    mkdir -p ${INSTALL_DIR}

    # 检查配置文件是否存在
    if [ ! -f "${CONFIG_FILE}" ]; then
        echo -e "${RED}❌ 未找到配置文件 ${CONFIG_FILE}，请先完成初始安装。${PLAIN}"
        return
    fi

    # 重新安装依赖
    install_python_deps

    # 如果代码文件不存在则拉取
    if [ ! -f "${INSTALL_DIR}/autoupdate_bot.py" ]; then
        do_fetch_code
    fi

    # 重新生成 service 文件（确保使用 venv Python）
    do_deploy_service

    echo -e "${GREEN}✅ 修复完成！服务已使用虚拟环境 Python 重新启动。${PLAIN}"
    echo -e "${GREEN}💡 请在 Telegram 中发送 /status 确认「自动巡检」显示为 🟢 运行中。${PLAIN}"
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
    echo -e "虚拟环境: ${VENV_DIR}"
    echo "-----------------------------------------"
    echo " 1. 查看运行状态 / 日志"
    echo " 2. 更新 Bot 程序代码（含重建服务配置）"
    echo " 3. 修改 Token / Chat ID 配置"
    echo " 4. 重启 Bot 服务"
    echo " 5. 停止 Bot 服务"
    echo " 6. 彻底卸载程序 (清除所有文件)"
    echo " 7. 修复安装（重建 venv/依赖/service）"
    echo " 0. 退出菜单"
    echo "-----------------------------------------"
    read -p "请输入数字选择 [0-7]: " choice

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
        7) repair_installation ;;
        0) exit 0 ;;
        *) echo -e "${RED}输入错误，请输入有效数字！${PLAIN}" ;;
    esac
}

if [ ! -f "${CONFIG_FILE}" ] || [ ! -f "${SERVICE_FILE}" ]; then
    install_bot
else
    show_menu
fi
