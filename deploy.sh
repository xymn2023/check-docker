#!/usr/bin/env bash

# =========================================================
# 项目: Docker 镜像自动更新 Telegram Bot 一键部署脚本
# =========================================================

set -e

# 定义安装路径与文件
INSTALL_DIR="/opt/docker-update-bot"
ENV_FILE="${INSTALL_DIR}/.env"
MAIN_SCRIPT="autoupdate_bot.py"
WATCHDOG_SCRIPT="watchdog.py"

# GitHub 仓库原生文件下载 Base URL (请替换为你自己的 GitHub 账号和仓库名)
GITHUB_RAW_URL="https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME/main"

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

# 2. 检查并安装必备基础环境
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

    # 检查 Python3
    if command -v python3 &> /dev/null; then
        INFO "检测到 Python3 已存在，跳过安装。"
    else
        WARN "未检测到 Python3，正在自动安装..."
        if command -v apt-get &> /dev/null; then
            apt-get update && apt-get install -y python3 python3-pip python3-venv curl
        elif command -v yum &> /dev/null; then
            yum install -y python3 python3-pip curl
        else
            ERROR "未找到支持的包管理器 (apt/yum)，请手动安装 python3。"
            exit 1
        fi
        INFO "Python3 安装完成！"
    fi

    # 检查 pip
    if ! command -v pip3 &> /dev/null && ! python3 -m pip --version &> /dev/null; then
        WARN "正在安装 python3-pip..."
        if command -v apt-get &> /dev/null; then
            apt-get install -y python3-pip
        elif command -v yum &> /dev/null; then
            yum install -y python3-pip
        fi
    fi

    INFO "正在检查并安装 Python 依赖库..."
    python3 -m pip install --upgrade pip -q
    python3 -m pip install python-telegram-bot requests pyyaml -q
    INFO "Python 依赖安装完成！"
}

# 3. 交互式获取并验证用户输入
get_user_input() {
    mkdir -p "${INSTALL_DIR}"

    echo -e "\n--------------------------------------------------"
    INFO "请输入您的 Telegram 配置参数："
    echo -e "--------------------------------------------------\n"

    # 获取 Telegram Bot Token (未输入则循环等待)
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

    # 获取 Chat ID (未输入则循环等待)
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

    # 将配置写入本地环境变量文件
    cat <<EOF > "${ENV_FILE}"
TELEGRAM_BOT_TOKEN="${BOT_TOKEN}"
ALLOWED_CHAT_ID="${CHAT_ID}"
EOF
    chmod 600 "${ENV_FILE}"
    INFO "配置参数已成功保存！"
}

# 4. 从 GitHub 下载程序文件
download_files() {
    INFO "正在从 GitHub 下载 Bot 主程序与守护自检程序..."

    # 如果在本地已有源码则直接复制，否则从远程 GitHub Raw 下载
    if [[ -f "./${MAIN_SCRIPT}" && -f "./${WATCHDOG_SCRIPT}" ]]; then
        INFO "检测到当前目录存在本地源码，直接部署..."
        cp "./${MAIN_SCRIPT}" "${INSTALL_DIR}/"
        cp "./${WATCHDOG_SCRIPT}" "${INSTALL_DIR}/"
    else
        INFO "正在从 GitHub 仓库拉取最新代码..."
        curl -fsSL "${GITHUB_RAW_URL}/${MAIN_SCRIPT}" -o "${INSTALL_DIR}/${MAIN_SCRIPT}"
        curl -fsSL "${GITHUB_RAW_URL}/${WATCHDOG_SCRIPT}" -o "${INSTALL_DIR}/${WATCHDOG_SCRIPT}"
    fi

    if [[ ! -f "${INSTALL_DIR}/${MAIN_SCRIPT}" || ! -f "${INSTALL_DIR}/${WATCHDOG_SCRIPT}" ]]; then
        ERROR "核心脚本文件下载失败！请检查网络或 GitHub 仓库路径。"
        exit 1
    fi

    INFO "程序代码下载/部署完毕！"
}

# 5. 后台后台守护启动 (托管至 Systemd 确保离线持久运行)
start_services() {
    INFO "正在配置 Systemd 后台守护服务..."

    # 写入 Systemd 服务文件 (运行 watchdog.py，由 watchdog 自动管理主程序)
    cat <<EOF > /etc/systemd/system/docker-update-bot.service
[Unit]
Description=Docker Auto Update Telegram Bot Watchdog Service
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/${WATCHDOG_SCRIPT}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    # 重载 systemd 并启动服务
    systemctl daemon-reload
    systemctl enable docker-update-bot.service
    systemctl restart docker-update-bot.service

    INFO "服务启动成功！已配置开机自启与后台无缝守护。"
}

# 6. 完全卸载功能
uninstall() {
    WARN "您确定要完全卸载 Docker 镜像更新 Bot 吗？"
    read -r -p "⚠️ 输入 'y' 或 'Y' 确认卸载，其他任意键取消: " confirm
    if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
        INFO "取消卸载。"
        exit 0
    fi

    INFO "正在停止并彻底移除服务..."

    # 停止并禁用 Systemd 服务
    if systemctl is-active --quiet docker-update-bot.service 2>/dev/null; then
        systemctl stop docker-update-bot.service
    fi
    if systemctl is-enabled --quiet docker-update-bot.service 2>/dev/null; then
        systemctl disable docker-update-bot.service
    fi

    # 清除残留的 Python 挂载进程 (如果有)
    pkill -f "python3 ${INSTALL_DIR}/${MAIN_SCRIPT}" || true
    pkill -f "python3 ${INSTALL_DIR}/${WATCHDOG_SCRIPT}" || true

    # 删除 systemd 服务配置文件
    rm -f /etc/systemd/system/docker-update-bot.service
    systemctl daemon-reload

    # 删除安装目录及所有相关文件
    if [[ -d "${INSTALL_DIR}" ]]; then
        rm -rf "${INSTALL_DIR}"
        INFO "已删除安装目录及文件: ${INSTALL_DIR}"
    fi

    INFO "是否同时卸载通过 pip 安装的 Python 依赖库 (python-telegram-bot, pyyaml, requests)？"
    read -r -p "输入 'y' 卸载依赖，输入其他回车跳过: " remove_pip
    if [[ "${remove_pip}" == "y" || "${remove_pip}" == "Y" ]]; then
        python3 -m pip uninstall -y python-telegram-bot pyyaml requests || true
        INFO "Python 扩展依赖库已清理。"
    fi

    INFO "✅ 彻底卸载完成！"
    exit 0
}

# 7. 查看运行日志
show_logs() {
    INFO "正在获取程序后台运行日志 (按 Ctrl+C 退出查看)..."
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
    echo " 1. 安装 / 重新部署程序"
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
            INFO "🎉 部署安装全部完成！您现在可以关闭终端窗口，Bot 会在后台静默运行。"
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

# 脚本直接执行入口
main_menu