#!/usr/bin/env bash

# =========================================================
# 项目: Docker 镜像自动更新 Telegram Bot 一键部署脚本
# 仓库: https://github.com/xymn2023/check-docker
# =========================================================

set -e

# 定义安装路径与文件
INSTALL_DIR="/opt/docker-update-bot"
VENV_DIR="${INSTALL_DIR}/venv"
ENV_FILE="${INSTALL_DIR}/.env"
MAIN_SCRIPT="autoupdate_bot.py"
WATCHDOG_SCRIPT="watchdog.py"

# GitHub 仓库原生文件下载 Base URL
GITHUB_RAW_URL="https://raw.githubusercontent.com/xymn2023/check-docker/master"

# 颜色输出控制
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
PLAIN='\033[0m'

INFO() { echo -e "${GREEN}[INFO]${PLAIN} $1"; }
WARN() { echo -e "${YELLOW}[WARN]${PLAIN} $1"; }
ERROR() { echo -e "${RED}[ERROR]${PLAIN} $1"; }

# 1. 权限检查
check_root() {
    if [[ $EUID -ne 0 ]]; then
       ERROR "必须使用 root 权限运行此脚本！请尝试使用 'sudo bash deploy.sh'"
       exit 1
    fi
}

# 2. 检查并安装必备基础环境 & 创建虚拟环境
check_and_install_env() {
    INFO "正在检查必备系统环境..."

    # 检查 Docker
    if command -v docker &> /dev/null; then
        INFO "检测到 Docker 已存在，跳过安装。"
    else
        WARN "未检测到 Docker，正在自动安装 Docker..."
        curl -fsSL https://get.docker.com | sh
        systemctl enable --now docker
        INFO "Docker 安装完成！"
    fi

    # 检查 Python3 并强制安装 python3-venv 和 python3-pip
    INFO "正在检查 Python3 及虚拟环境组件..."
    if command -v apt-get &> /dev/null; then
        apt-get update -q
        apt-get install -y python3 python3-pip python3-venv curl -q
    elif command -v yum &> /dev/null; then
        yum install -y python3 python3-pip curl -q
    else
        if ! command -v python3 &> /dev/null; then
            ERROR "未找到支持的包管理器 (apt/yum)，请手动安装 python3。"
            exit 1
        fi
    fi

    mkdir -p "${INSTALL_DIR}"

    # 创建隔离的虚拟环境 (核心修复: 解决 PEP 668 报错)
    if [[ ! -d "${VENV_DIR}" ]]; then
        INFO "正在创建 Python 虚拟环境 (venv)..."
        python3 -m venv "${VENV_DIR}"
        INFO "虚拟环境创建成功：${VENV_DIR}"
    else
        INFO "检测到已存在 Python 虚拟环境，跳过创建。"
    fi

    # 在虚拟环境内安装扩展依赖
    INFO "正在虚拟环境中安装 Python 依赖库..."
    "${VENV_DIR}/bin/python" -m pip install --upgrade pip -q
    "${VENV_DIR}/bin/python" -m pip install python-telegram-bot requests pyyaml -q
    INFO "虚拟环境依赖库安装完成！"
}

# 3. 交互式获取并验证用户配置 (死循环等待直至输入)
get_user_input() {
    echo -e "\n--------------------------------------------------"
    INFO "请输入您的 Telegram 配置参数："
    echo -e "--------------------------------------------------\n"

    # 获取 Telegram Bot Token
    while true; do
        read -r -p "👉 请输入 Telegram Bot Token: " input_token
        if [[ -n "${input_token}" ]]; then
            echo -e "您输入的 Token 是: ${YELLOW}${input_token}${PLAIN}"
            read -r -p "按 [Enter/回车键] 确认并继续，或按 Ctrl+C 重新运行..."
            BOT_TOKEN="${input_token}"
            break
        else
            WARN "Token 不能为空，请输入后再按回车！"
        fi
    done

    echo ""

    # 获取 Chat ID
    while true; do
        read -r -p "👉 请输入您的 Telegram Chat ID: " input_chat_id
        if [[ -n "${input_chat_id}" ]]; then
            echo -e "您输入的 Chat ID 是: ${YELLOW}${input_chat_id}${PLAIN}"
            read -r -p "按 [Enter/回车键] 确认并继续，或按 Ctrl+C 重新运行..."
            CHAT_ID="${input_chat_id}"
            break
        else
            WARN "Chat ID 不能为空，请输入后再按回车！"
        fi
    done

    # 写入环境变量保存文件
    cat <<EOF > "${ENV_FILE}"
TELEGRAM_BOT_TOKEN="${BOT_TOKEN}"
ALLOWED_CHAT_ID="${CHAT_ID}"
EOF
    chmod 600 "${ENV_FILE}"
    INFO "配置参数已成功保存！"
}

# 4. 下载程序源码
download_files() {
    INFO "正在下载主程序与守护自检程序..."

    if [[ -f "./${MAIN_SCRIPT}" && -f "./${WATCHDOG_SCRIPT}" ]]; then
        INFO "检测到当前目录存在本地源码，使用本地文件部署..."
        cp "./${MAIN_SCRIPT}" "${INSTALL_DIR}/"
        cp "./${WATCHDOG_SCRIPT}" "${INSTALL_DIR}/"
    else
        INFO "正在从 GitHub 仓库拉取最新代码..."
        curl -fsSL "${GITHUB_RAW_URL}/${MAIN_SCRIPT}" -o "${INSTALL_DIR}/${MAIN_SCRIPT}"
        curl -fsSL "${GITHUB_RAW_URL}/${WATCHDOG_SCRIPT}" -o "${INSTALL_DIR}/${WATCHDOG_SCRIPT}"
    fi

    if [[ ! -f "${INSTALL_DIR}/${MAIN_SCRIPT}" || ! -f "${INSTALL_DIR}/${WATCHDOG_SCRIPT}" ]]; then
        ERROR "核心脚本下载失败！请检查 GitHub 仓库文件状态。"
        exit 1
    fi

    INFO "程序代码部署完成！"
}

# 5. 配置 Systemd 后台服务 (指定 venv 解释器)
start_services() {
    INFO "正在配置 Systemd 后台守护服务..."

    cat <<EOF > /etc/systemd/system/docker-update-bot.service
[Unit]
Description=Docker Auto Update Telegram Bot Watchdog Service (Venv)
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/${WATCHDOG_SCRIPT}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable docker-update-bot.service
    systemctl restart docker-update-bot.service

    INFO "服务启动成功！已托管至 Systemd 后台运行。"
}

# 6. 完全卸载功能
uninstall() {
    WARN "您确定要完全卸载 Docker 镜像更新 Bot 吗？"
    read -r -p "⚠️ 输入 'y' 或 'Y' 确认卸载，其他任意键取消: " confirm
    if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
        INFO "取消卸载。"
        exit 0
    fi

    INFO "正在停止服务并删除相关文件..."

    if systemctl is-active --quiet docker-update-bot.service 2>/dev/null; then
        systemctl stop docker-update-bot.service
    fi
    if systemctl is-enabled --quiet docker-update-bot.service 2>/dev/null; then
        systemctl disable docker-update-bot.service
    fi

    pkill -f "${INSTALL_DIR}" || true

    rm -f /etc/systemd/system/docker-update-bot.service
    systemctl daemon-reload

    if [[ -d "${INSTALL_DIR}" ]]; then
        rm -rf "${INSTALL_DIR}"
        INFO "已删除安装目录及虚拟环境: ${INSTALL_DIR}"
    fi

    INFO "✅ 彻底卸载完成！"
    exit 0
}

# 7. 查看运行日志
show_logs() {
    INFO "正在获取后台运行日志 (按 Ctrl+C 退出)..."
    journalctl -u docker-update-bot.service -f -n 50
}

# 8. 主菜单交互入口
main_menu() {
    clear
    echo -e "${GREEN}"
    echo "=================================================="
    echo "  Docker 镜像自动更新 Telegram Bot 一键管理脚本  "
    echo "=================================================="
    echo -e "${PLAIN}"
    echo " 1. 安装 / 重新部署程序 (自动配置 venv 虚拟环境)"
    echo " 2. 查看后台运行日志"
    echo " 3. 重启程序服务"
    echo " 4. 停止程序服务"
    echo " 5. 卸载程序及关联环境"
    echo " 0. 退出脚本"
    echo ""
    read -r -p "请输入数字选择操作 [0-5]: " num

    case "${num}" in
        1)
            check_root
            check_and_install_env
            get_user_input
            download_files
            start_services
            INFO "🎉 部署安装全部完成！程序正在 Python 虚拟环境中运行，可安全关闭终端。"
            ;;
        2)
            show_logs
            ;;
        3)
            check_root
            systemctl restart docker-update-bot.service
            INFO "服务重启指令已下发！"
            ;;
        4)
            check_root
            systemctl stop docker-update-bot.service
            INFO "服务已停止！"
            ;;
        5)
            check_root
            uninstall
            ;;
        0)
            exit 0
            ;;
        *)
            ERROR "请输入正确的选项 [0-5]"
            ;;
    esac
}

main_menu
