import os
import sys
import time
import json
import re
import subprocess
import asyncio
import logging
from datetime import datetime
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# 配置日志输出
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

DATA_DIR = "/opt/docker-update-bot"
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
VERSION_FILE = os.path.join(DATA_DIR, ".version")
UPDATING_MARKER_FILE = os.path.join(DATA_DIR, ".updating_msg")
RESTART_TRIGGER_FILE = os.path.join(DATA_DIR, ".need_restart")  # 外部解耦重启信号

# GitHub 仓库源码路径
GITHUB_RAW_URL = "https://raw.githubusercontent.com/xymn2023/check-docker/main"
GITHUB_API_URL = "https://api.github.com/repos/xymn2023/check-docker/commits/main"

CHECK_INTERVAL = 3600  # 定时巡检间隔 (秒)

monitored_images = set()
scan_temp_state = {}
is_updating = False
last_check_time = "尚未执行"
TELEGRAM_BOT_TOKEN = ""
ALLOWED_CHAT_ID = ""


def load_config() -> bool:
    """读取本地配置文件或环境变量"""
    global TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_ID

    env_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    env_chat = os.getenv("ALLOWED_CHAT_ID", "")

    if env_token and env_chat:
        TELEGRAM_BOT_TOKEN = env_token
        ALLOWED_CHAT_ID = str(env_chat).strip()
        return True

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                TELEGRAM_BOT_TOKEN = cfg.get("bot_token", "").strip()
                ALLOWED_CHAT_ID = str(cfg.get("chat_id", "")).strip()
                if TELEGRAM_BOT_TOKEN and ALLOWED_CHAT_ID:
                    logging.info("成功读取本地 config.json 配置文件！")
                    return True
        except Exception as e:
            logging.error(f"读取 config.json 失败: {e}")

    print("\n❌ 错误：未检测到有效配置！请重新生成配置文件。\n")
    return False


def load_tasks_from_disk():
    """从磁盘加载保存的监控镜像任务"""
    global monitored_images
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    monitored_images = set(data)
        except Exception as e:
            logging.error(f"读取 tasks.json 失败: {e}")


def save_tasks_to_disk():
    """保存监控镜像任务到磁盘"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(monitored_images), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"保存 tasks.json 失败: {e}")


def run_cmd(cmd: str) -> tuple[int, str]:
    """执行 Shell 命令工具函数"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        return result.returncode, result.stdout.strip() + "\n" + result.stderr.strip()
    except Exception as e:
        return -1, str(e)


def check_docker_daemon() -> bool:
    """检查 Docker 守护进程状态"""
    code, _ = run_cmd("docker info")
    return code == 0


def get_running_docker_images() -> list[str]:
    """获取当前所有运行中容器的镜像"""
    code, out = run_cmd("docker ps --format '{{.Image}}'")
    if code != 0 or not out.strip():
        return []

    valid_images = []
    for line in out.split("\n"):
        img = line.strip()
        if img and "<none>" not in img:
            if ":" not in img.split("/")[-1]:
                img = f"{img}:latest"
            valid_images.append(img)

    return list(dict.fromkeys(valid_images))


def get_image_digest(image_name: str) -> str:
    """获取镜像摘要 Hash"""
    code, out = run_cmd(f"docker inspect --format='{{{{index .RepoDigests 0}}}}' {image_name}")
    return out.split("\n")[0] if code == 0 and out else ""


def build_scan_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    """构建扫描镜像勾选菜单"""
    state = scan_temp_state.get(chat_id, {})
    keyboard = []

    for idx, (img, checked) in enumerate(state.items()):
        icon = "☑️" if checked else "⬜"
        keyboard.append([InlineKeyboardButton(f"{icon} {img}", callback_data=f"toggle:{idx}")])

    keyboard.append([
        InlineKeyboardButton("☑️ 全部勾选", callback_data="select_all"),
        InlineKeyboardButton("⬜ 全部取消", callback_data="deselect_all")
    ])
    keyboard.append([InlineKeyboardButton("🚀 确认并保存监控任务", callback_data="save_tasks")])
    return InlineKeyboardMarkup(keyboard)


async def check_is_owner(update: Update) -> bool:
    """权限校验锁：仅 ALLOWED_CHAT_ID 可操作"""
    if not update or not update.effective_chat:
        return False

    incoming_chat_id = str(update.effective_chat.id)

    if incoming_chat_id != str(ALLOWED_CHAT_ID):
        user_info = update.effective_user.username if update.effective_user else "Unknown"
        logging.warning(f"⛔ [拦截非法访问] ChatID: {incoming_chat_id} (User: @{user_info})")

        if update.message:
            await update.message.reply_text("⛔ *未授权*：您无权限使用此 Bot！", parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.answer("⛔ 未授权：禁止操作！", show_alert=True)

        return False

    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_is_owner(update): return
    await update.message.reply_text(
        "👋 *欢迎使用 Docker 镜像自动更新 Bot！*\n\n"
        "👑 *身份验证成功：管理员账号*\n\n"
        "可用命令：\n"
        "• /scan - 扫描当前运行的 Docker 镜像并设置监控\n"
        "• /check - 立即触发一轮镜像更新巡检\n"
        "• /status - 查看当前监控池与系统运行状态\n"
        "• /update - 强制升级并重启 Bot 程序",
        parse_mode="Markdown"
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_is_owner(update): return
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🔎 正在实时检索服务器运行中的 Docker 容器镜像...", parse_mode="Markdown")

    if not check_docker_daemon():
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="❌ *Docker 引擎未启动或无法响应！*", parse_mode="Markdown")
        return

    current_running_images = get_running_docker_images()

    if not current_running_images:
        scan_temp_state[str(chat_id)] = {}
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="⚠️ 未检测到当前服务器上有任何运行中的 Docker 容器。")
        return

    new_scan_state = {}
    for img in current_running_images:
        new_scan_state[img] = (img in monitored_images)

    scan_temp_state[str(chat_id)] = new_scan_state

    reply_markup = build_scan_keyboard(str(chat_id))
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg.message_id,
        text=f"🔍 *实时扫描到当前运行中 {len(current_running_images)} 个标准镜像*\n\n(已自动剔除隐式 ID 与悬空镜像)\n请勾选需要自动检测更新的镜像：",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_is_owner(update): return
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat_id)

    data = query.data
    state = scan_temp_state.get(chat_id, {})
    img_list = list(state.keys())

    if data.startswith("toggle:"):
        try:
            idx = int(data.split("toggle:", 1)[1])
            if 0 <= idx < len(img_list):
                target_img = img_list[idx]
                state[target_img] = not state[target_img]
                await query.edit_message_reply_markup(reply_markup=build_scan_keyboard(chat_id))
        except (ValueError, IndexError):
            pass

    elif data == "select_all":
        for img in state: state[img] = True
        await query.edit_message_reply_markup(reply_markup=build_scan_keyboard(chat_id))

    elif data == "deselect_all":
        for img in state: state[img] = False
        await query.edit_message_reply_markup(reply_markup=build_scan_keyboard(chat_id))

    elif data == "save_tasks":
        global monitored_images
        monitored_images = {img for img, checked in state.items() if checked}
        save_tasks_to_disk()

        selected_text = "\n".join([f"• `{img}`" for img in monitored_images]) if monitored_images else "（当前未选择任何任务）"
        await query.edit_message_text(
            f"✅ *监控任务池已成功同步保存！*\n\n📌 当前已激活监控 ({len(monitored_images)} 个):\n{selected_text}",
            parse_mode="Markdown"
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_is_owner(update): return
    docker_ok = "🟢 正常响应" if check_docker_daemon() else "🔴 异常或未启动"
    selected_text = "\n".join([f"• `{img}`" for img in monitored_images]) if monitored_images else "（未配置）"
    status_str = "🔄 更新中..." if is_updating else "💤 待命巡检"

    msg = (
        f"📊 *Docker 镜像自动更新服务状态*\n-----------------------------\n"
        f"⚙️ Docker 引擎: *{docker_ok}*\n⏱️ 上次检测: `{last_check_time}`\n📌 状态: *{status_str}*\n\n"
        f"🎯 *当前保存生效的监控任务 ({len(monitored_images)} 个):*\n{selected_text}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_update_self(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_is_owner(update): return
    msg = await update.message.reply_text("🔎 *⚙️ [1/4] 正在连接 GitHub API 校验远程代码版本...*", parse_mode="Markdown")

    # 1. 校验 API
    code, api_out = run_cmd(f"curl -s -m 10 {GITHUB_API_URL}")
    if code != 0 or '"sha"' not in api_out:
        await context.bot.edit_message_text(chat_id=ALLOWED_CHAT_ID, message_id=msg.message_id, text="❌ 无法连接 GitHub API 或网络超时，更新已取消。")
        return

    try:
        remote_sha = json.loads(api_out)["sha"][:7]
    except Exception:
        remote_sha = "unknown"

    local_sha = ""
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, "r") as f: local_sha = f.read().strip()

    if local_sha == remote_sha and local_sha != "":
        await context.bot.edit_message_text(
            chat_id=ALLOWED_CHAT_ID, message_id=msg.message_id,
            text=f"🟢 *当前已是最新版本，无需升级！*\n📌 本地 Hash: `{local_sha}`\n📌 远程 Hash: `{remote_sha}`", parse_mode="Markdown"
        )
        return

    save_tasks_to_disk()
    await context.bot.edit_message_text(chat_id=ALLOWED_CHAT_ID, message_id=msg.message_id, text=f"🚀 *⚙️ [2/4] 检测到新版本 ({remote_sha})，正在拉取最新源码...*", parse_mode="Markdown")

    # 2. 覆盖源码
    code1, _ = run_cmd(f"curl -fsSL -m 15 {GITHUB_RAW_URL}/autoupdate_bot.py -o {DATA_DIR}/autoupdate_bot.py")
    if code1 != 0:
        await context.bot.edit_message_text(chat_id=ALLOWED_CHAT_ID, message_id=msg.message_id, text="❌ 源码拉取失败，请检查服务器网络状态。")
        return

    # 3. 写入最新 SHA
    with open(VERSION_FILE, "w") as f: f.write(remote_sha)

    # 4. 写入 Telegram 履约标记文件
    try:
        with open(UPDATING_MARKER_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "chat_id": ALLOWED_CHAT_ID,
                "message_id": msg.message_id,
                "sha": remote_sha,
                "old_pid": os.getpid(),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }, f)
    except Exception as e:
        logging.error(f"写入履约标记文件失败: {e}")

    # 5. 生成重启信号标记文件（通知保活/Watchdog 脚本进行真正安全的外部重启）
    with open(RESTART_TRIGGER_FILE, "w") as f:
        f.write("restart")

    # 6. 更新 Telegram 提示
    await context.bot.edit_message_text(
        chat_id=ALLOWED_CHAT_ID,
        message_id=msg.message_id,
        text=f"⚙️ *[3/4] 代码已完成覆盖！*\n🔄 已向系统 Watchdog 发出重启信号，当前进程立即退出...",
        parse_mode="Markdown"
    )

    await asyncio.sleep(1)

    # 7. 主动正常退出，彻底把掌控权交给外部脚本
    sys.exit(0)


async def execute_update_check(context: ContextTypes.DEFAULT_TYPE, manual: bool = False):
    global is_updating, last_check_time
    if is_updating:
        if manual: await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="⚠️ 当前已有检测任务在进行中，请勿重复触发。")
        return

    if not check_docker_daemon():
        await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="🚨 Docker 引擎未响应或已挂起！")
        return

    if not monitored_images:
        if manual: await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="⚠️ 监控任务池为空，请先使用 /scan 命令设置需要监控的镜像。")
        return

    is_updating = True
    last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    images_list = list(monitored_images)
    total_count = len(images_list)
    progress_msg = None

    if manual:
        progress_msg = await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=f"🚀 *开始执行镜像拉取与检测任务 ({total_count} 个)...*", parse_mode="Markdown")

    results_summary = []
    try:
        for idx, img in enumerate(images_list, 1):
            old_digest = get_image_digest(img)
            pull_code, _ = run_cmd(f"docker pull {img}")
            new_digest = get_image_digest(img)

            if pull_code == 0 and old_digest != new_digest:
                code, container_names = run_cmd(f"docker ps -q --filter ancestor={img} | xargs -r docker inspect --format '{{{{.Name}}}}'")
                clean_names = [name.lstrip("/") for name in container_names.split("\n") if name.strip()]

                failed = False
                for c_name in clean_names:
                    r_code, _ = run_cmd(f"docker restart {c_name}")
                    if r_code != 0: failed = True

                res_str = f"✅ `{img}` -> 已检测到新版本，镜像已更新，相关容器均已重启成功" if not failed else f"⚠️ `{img}` -> 镜像已更新，但部分容器重启失败"
            else:
                res_str = f"🟢 `{img}` -> 暂无更新 (已是最新)"

            results_summary.append(res_str)
            await asyncio.sleep(0.5)

        summary_text = f"🏁 *巡检任务执行完成！*\n\n" + "\n".join(results_summary)
        if manual and progress_msg:
            await context.bot.edit_message_text(chat_id=ALLOWED_CHAT_ID, message_id=progress_msg.message_id, text=summary_text, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"更新异常: {e}")
    finally:
        is_updating = False


async def cmd_manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_is_owner(update): return
    await execute_update_check(context, manual=True)


async def handle_unknown_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await check_is_owner(update)


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    await execute_update_check(context, manual=False)


async def post_init(application: Application):
    load_tasks_from_disk()

    # 重启成功履约检查
    if os.path.exists(UPDATING_MARKER_FILE):
        try:
            with open(UPDATING_MARKER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            chat_id = data.get("chat_id")
            message_id = data.get("message_id")
            sha = data.get("sha", "unknown")
            old_pid = data.get("old_pid", "unknown")
            new_pid = os.getpid()

            docker_status = "🟢 正常" if check_docker_daemon() else "🔴 异常"

            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"🎉 *[4/4] 外部 Watchdog 重启服务成功！*\n-----------------------------\n"
                    f"📌 Commit Hash: `{sha}`\n"
                    f"💀 原进程 PID: `{old_pid}` (已退出)\n"
                    f"🚀 新进程 PID: `{new_pid}` (运行中)\n"
                    f"⚙️ Docker 环境: *{docker_status}*\n"
                    f"⏱️ 启动时间: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"发送重启成功反馈失败: {e}")
        finally:
            if os.path.exists(UPDATING_MARKER_FILE):
                os.remove(UPDATING_MARKER_FILE)

    await application.bot.set_my_commands([
        BotCommand("scan", "扫描容器镜像"),
        BotCommand("check", "检测镜像更新"),
        BotCommand("update", "升级程序自身"),
        BotCommand("status", "查看运行状态"),
    ])


def main():
    if not load_config():
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("check", cmd_manual_check))
    app.add_handler(CommandHandler("update", cmd_update_self))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    app.add_handler(MessageHandler(filters.ALL, handle_unknown_messages))

    if app.job_queue:
        app.job_queue.run_repeating(scheduled_job, interval=CHECK_INTERVAL, first=30)

    app.run_polling()


if __name__ == "__main__":
    main()