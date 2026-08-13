import os
import sys
import time
import subprocess
import asyncio
import logging
from datetime import datetime
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# 配置日志格式
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ==================== 配置区域 ====================
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # 替换为你的 Token
ALLOWED_CHAT_ID = "YOUR_CHAT_ID_HERE"        # 替换为你的 Chat ID

CHECK_INTERVAL = 3600  # 自动巡检间隔（秒）
# ==================================================

monitored_images = set()
scan_temp_state = {}
is_updating = False
last_check_time = "尚未执行"


def run_cmd(cmd: str) -> tuple[int, str]:
    """带有超时保护的命令执行"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        return result.returncode, result.stdout.strip() + "\n" + result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "Command Execution Timeout (120s)"
    except Exception as e:
        return -1, str(e)


def check_docker_daemon() -> bool:
    """自检 1：检测 Docker Daemon 是否正常响应"""
    code, _ = run_cmd("docker info")
    return code == 0


def get_running_docker_images() -> list[str]:
    """读取当前服务器运行的镜像"""
    code, out = run_cmd("docker ps --format '{{.Image}}'")
    if code != 0 or not out.strip():
        return []
    raw_images = [img.strip() for img in out.split("\n") if img.strip()]
    return list(dict.fromkeys(raw_images))


def get_image_digest(image_name: str) -> str:
    code, out = run_cmd(f"docker inspect --format='{{{{index .RepoDigests 0}}}}' {image_name}")
    if code == 0 and out:
        return out.split("\n")[0]
    return ""


def build_scan_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    state = scan_temp_state.get(chat_id, {})
    keyboard = []
    for img, checked in state.items():
        icon = "☑️" if checked else "⬜"
        keyboard.append([InlineKeyboardButton(f"{icon} {img}", callback_data=f"toggle:{img}")])

    keyboard.append([
        InlineKeyboardButton("☑️ 全部勾选", callback_data="select_all"),
        InlineKeyboardButton("⬜ 全部取消", callback_data="deselect_all")
    ])
    keyboard.append([InlineKeyboardButton("🚀 确认并保存监控任务", callback_data="save_tasks")])
    return InlineKeyboardMarkup(keyboard)


async def auth_check(update: Update) -> bool:
    if str(update.effective_chat.id) != str(ALLOWED_CHAT_ID):
        if update.message:
            await update.message.reply_text("⛔ 无权使用此 Bot。")
        return False
    return True


# ==================== 指令交互逻辑 ====================

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    chat_id = update.effective_chat.id

    if not check_docker_daemon():
        await update.message.reply_text("❌ *环境自检失败*：Docker 守护进程未启动或无法响应！", parse_mode="Markdown")
        return

    images = get_running_docker_images()
    if not images:
        await update.message.reply_text("⚠️ 未检测到当前服务器上有运行中的 Docker 容器。")
        return

    scan_temp_state[chat_id] = {img: True for img in images}
    reply_markup = build_scan_keyboard(chat_id)
    await update.message.reply_text(
        f"🔍 *扫描到当前服务器正在运行 {len(images)} 个镜像*\n\n请在下方勾选需要**自动检测更新**的镜像：",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if str(chat_id) != str(ALLOWED_CHAT_ID): return

    data = query.data
    state = scan_temp_state.get(chat_id, {})

    if data.startswith("toggle:"):
        img = data.split("toggle:", 1)[1]
        if img in state:
            state[img] = not state[img]
            await query.edit_message_reply_markup(reply_markup=build_scan_keyboard(chat_id))

    elif data == "select_all":
        for img in state: state[img] = True
        await query.edit_message_reply_markup(reply_markup=build_scan_keyboard(chat_id))

    elif data == "deselect_all":
        for img in state: state[img] = False
        await query.edit_message_reply_markup(reply_markup=build_scan_keyboard(chat_id))

    elif data == "save_tasks":
        global monitored_images
        monitored_images = {img for img, checked in state.items() if checked}
        selected_text = "\n".join([f"• `{img}`" for img in monitored_images]) if monitored_images else "（无）"
        await query.edit_message_text(
            f"✅ *自动检测更新任务池配置成功！*\n\n📌 当前监控列表 ({len(monitored_images)} 个):\n{selected_text}\n\n⏱️ Bot 将每隔 `{CHECK_INTERVAL}s` 自动检查上述镜像并推送更新。",
            parse_mode="Markdown"
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    
    docker_ok = "🟢 正常响应" if check_docker_daemon() else "🔴 异常或未启动"
    selected_text = "\n".join([f"• `{img}`" for img in monitored_images]) if monitored_images else "（当前未配置任何监控镜像）"
    status_str = "🔄 正在更新中..." if is_updating else "💤 待命巡检中"

    msg = (
        f"📊 *Docker 镜像自动更新服务状态*\n"
        f"-----------------------------\n"
        f"⚙️ Docker 引擎状态: *{docker_ok}*\n"
        f"⏱️ 上次检测时间: `{last_check_time}`\n"
        f"📌 当前运行状态: *{status_str}*\n\n"
        f"🎯 *正在监控更新的镜像列表 ({len(monitored_images)} 个):*\n"
        f"{selected_text}\n\n"
        f"💡 使用 /scan 命令可随时增删监控任务。"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def execute_update_check(context: ContextTypes.DEFAULT_TYPE, manual: bool = False):
    global is_updating, last_check_time
    if is_updating:
        if manual:
            await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="⚠️ 当前已有更新任务在进行中，请稍后再试。")
        return

    # 自检 Docker 环境
    if not check_docker_daemon():
        await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="🚨 *警告*：检测到 Docker 守护进程宕机或未响应，更新任务暂停执行！", parse_mode="Markdown")
        return

    if not monitored_images:
        if manual:
            await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="⚠️ 任务池为空！请先使用 /scan 扫描并选择需要监控的镜像。")
        return

    is_updating = True
    last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        if manual:
            await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=f"🔍 *开始手动检测任务池中 {len(monitored_images)} 个镜像的更新...*", parse_mode="Markdown")

        for img in list(monitored_images):
            old_digest = get_image_digest(img)
            pull_code, _ = run_cmd(f"docker pull {img}")
            new_digest = get_image_digest(img)

            if pull_code == 0 and old_digest != new_digest:
                msg = await context.bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text=f"🔍 *检测到镜像有更新！*\n📦 镜像: `{img}`\n⏳ 正在拉取中...",
                    parse_mode="Markdown"
                )

                await asyncio.sleep(1)
                await context.bot.edit_message_text(
                    chat_id=ALLOWED_CHAT_ID,
                    message_id=msg.message_id,
                    text=f"🔄 *镜像 `{img}` 下载完成！*\n⚙️ 正在重启依赖该镜像的服务...",
                    parse_mode="Markdown"
                )

                code, container_names = run_cmd(f"docker ps -q --filter ancestor={img} | xargs -r docker inspect --format '{{{{.Name}}}}'")
                clean_names = [name.lstrip("/") for name in container_names.split("\n") if name.strip()]
                
                restart_failed = False
                for c_name in clean_names:
                    r_code, _ = run_cmd(f"docker restart {c_name}")
                    if r_code != 0: restart_failed = True

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if not restart_failed:
                    await context.bot.edit_message_text(
                        chat_id=ALLOWED_CHAT_ID,
                        message_id=msg.message_id,
                        text=f"✅ *镜像 `{img}` 自动更新并重启服务成功！*\n⏱️ 完成时间: `{now_str}`",
                        parse_mode="Markdown"
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=ALLOWED_CHAT_ID,
                        message_id=msg.message_id,
                        text=f"❌ *镜像 `{img}` 更新成功，但容器重启失败！*\n稍后将重试...",
                        parse_mode="Markdown"
                    )
    except Exception as e:
        logging.error(f"更新逻辑抛出异常: {e}")
    finally:
        is_updating = False


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    await execute_update_check(context, manual=False)


async def post_init(application: Application):
    """启动自检与成功通知"""
    commands = [
        BotCommand("scan", "扫描本地镜像并管理更新任务池"),
        BotCommand("check", "立即对任务池中的镜像检测更新"),
        BotCommand("status", "查看当前任务池与服务状态"),
    ]
    await application.bot.set_my_commands(commands)

    # 启动时检测 Docker
    docker_status = "🟢 正常" if check_docker_daemon() else "🔴 未响应"
    
    # 判断是否是崩溃重启（通过环境变量传递标识）
    is_reboot = os.getenv("IS_RESTART_EVENT", "false") == "true"
    
    title = "🔄 *Docker 监控 Bot 崩溃重启成功通知*" if is_reboot else "🚀 *Docker 监控 Bot 启动成功通知*"
    
    await application.bot.send_message(
        chat_id=ALLOWED_CHAT_ID,
        text=f"{title}\n"
             f"------------------------------------\n"
             f"⏱️ 启动时间: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
             f"⚙️ Docker 守护进程: *{docker_status}*\n"
             f"💡 发送 /scan 即可配置镜像监控任务。",
        parse_mode="Markdown"
    )


def main():
    # 增加网络重试，防止网络瞬断导致程序启动异常
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("check", lambda u, c: execute_update_check(c, manual=True)))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    if app.job_queue:
        app.job_queue.run_repeating(scheduled_job, interval=CHECK_INTERVAL, first=30)

    app.run_polling()


if __name__ == "__main__":
    main()