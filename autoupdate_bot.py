import os
import sys
import time
import json
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

DATA_DIR = "/opt/docker-update-bot"
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")

CHECK_INTERVAL = 3600

monitored_images = set()
scan_temp_state = {}
is_updating = False
last_check_time = "尚未执行"
TELEGRAM_BOT_TOKEN = ""
ALLOWED_CHAT_ID = ""


def load_config() -> bool:
    """读取配置"""
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
                    return True
        except Exception as e:
            logging.error(f"读取配置失败: {e}")

    print("\n❌ 未检测到有效的 Telegram 配置，请检查！\n")
    return False


def load_tasks_from_disk():
    global monitored_images
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    monitored_images = set(data)
        except Exception as e:
            logging.error(f"读取 tasks 失败: {e}")


def save_tasks_to_disk():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(monitored_images), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"保存 tasks 失败: {e}")


def run_cmd(cmd: str) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        return result.returncode, result.stdout.strip() + "\n" + result.stderr.strip()
    except Exception as e:
        return -1, str(e)


def check_docker_daemon() -> bool:
    code, _ = run_cmd("docker info")
    return code == 0


def get_running_docker_images() -> list[str]:
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
    code, out = run_cmd(f"docker inspect --format='{{{{index .RepoDigests 0}}}}' {image_name}")
    return out.split("\n")[0] if code == 0 and out else ""


def build_scan_keyboard(chat_id: str) -> InlineKeyboardMarkup:
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


# ----------------🔒 核心鉴权锁 ----------------
async def check_is_owner(update: Update) -> bool:
    """只有 ALLOWED_CHAT_ID 可以使用"""
    if not update or not update.effective_chat:
        return False

    incoming_chat_id = str(update.effective_chat.id)

    if incoming_chat_id != str(ALLOWED_CHAT_ID):
        logging.warning(f"⛔ 拒绝陌生人使用: {incoming_chat_id}")
        if update.message:
            await update.message.reply_text("⛔ *未授权*：您无权限使用此 Bot！", parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.answer("⛔ 未授权！", show_alert=True)
        return False

    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_is_owner(update): return
    await update.message.reply_text(
        "👋 *欢迎使用 Docker 镜像自动更新 Bot！*\n\n"
        "👑 *管理员权限校验通过*\n\n"
        "可用命令：\n"
        "• /scan - 扫描当前运行的 Docker 镜像并设置监控\n"
        "• /check - 立即触发一轮镜像更新巡检\n"
        "• /status - 查看当前监控池与系统运行状态",
        parse_mode="Markdown"
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_is_owner(update): return
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🔎 正在检索服务器运行中的 Docker 容器镜像...", parse_mode="Markdown")

    if not check_docker_daemon():
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="❌ *Docker 引擎未启动！*", parse_mode="Markdown")
        return

    current_running_images = get_running_docker_images()

    if not current_running_images:
        scan_temp_state[str(chat_id)] = {}
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="⚠️ 未检测到运行中的 Docker 容器。")
        return

    new_scan_state = {}
    for img in current_running_images:
        new_scan_state[img] = (img in monitored_images)

    scan_temp_state[str(chat_id)] = new_scan_state

    reply_markup = build_scan_keyboard(str(chat_id))
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg.message_id,
        text=f"🔍 *扫描到 {len(current_running_images)} 个镜像*\n请勾选需要自动监控更新的镜像：",
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

        selected_text = "\n".join([f"• `{img}`" for img in monitored_images]) if monitored_images else "（未选择任何任务）"
        await query.edit_message_text(
            f"✅ *监控任务池保存成功！*\n\n📌 当前监控 ({len(monitored_images)} 个):\n{selected_text}",
            parse_mode="Markdown"
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_is_owner(update): return
    docker_ok = "🟢 正常" if check_docker_daemon() else "🔴 异常"
    selected_text = "\n".join([f"• `{img}`" for img in monitored_images]) if monitored_images else "（未配置）"
    status_str = "🔄 更新中..." if is_updating else "💤 待命"

    msg = (
        f"📊 *Docker 镜像自动更新状态*\n-----------------------------\n"
        f"⚙️ Docker 引擎: *{docker_ok}*\n⏱️ 上次检测: `{last_check_time}`\n📌 状态: *{status_str}*\n\n"
        f"🎯 *当前监控池 ({len(monitored_images)} 个):*\n{selected_text}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def execute_update_check(context: ContextTypes.DEFAULT_TYPE, manual: bool = False):
    global is_updating, last_check_time
    if is_updating:
        if manual: await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="⚠️ 当前已有检测任务在进行中。")
        return

    if not check_docker_daemon():
        await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="🚨 Docker 引擎未响应！")
        return

    if not monitored_images:
        if manual: await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="⚠️ 监控任务池为空，请先使用 /scan 命令设置。")
        return

    is_updating = True
    last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    images_list = list(monitored_images)
    total_count = len(images_list)
    progress_msg = None

    if manual:
        progress_msg = await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=f"🚀 *开始执行检测任务 ({total_count} 个)...*", parse_mode="Markdown")

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

                res_str = f"✅ `{img}` -> 镜像已更新，相关容器已重启" if not failed else f"⚠️ `{img}` -> 镜像已更新，部分容器重启失败"
            else:
                res_str = f"🟢 `{img}` -> 暂无更新"

            results_summary.append(res_str)
            await asyncio.sleep(0.5)

        summary_text = f"🏁 *巡检任务完成！*\n\n" + "\n".join(results_summary)
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
    await application.bot.set_my_commands([
        BotCommand("scan", "扫描容器镜像"),
        BotCommand("check", "检测镜像更新"),
        BotCommand("status", "查看运行状态"),
    ])


def main():
    if not load_config():
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("check", cmd_manual_check))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    app.add_handler(MessageHandler(filters.ALL, handle_unknown_messages))

    if app.job_queue:
        app.job_queue.run_repeating(scheduled_job, interval=CHECK_INTERVAL, first=30)

    app.run_polling()


if __name__ == "__main__":
    main()