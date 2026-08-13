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

# 日志输出控制
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# 文件与目录配置
DATA_DIR = "/opt/docker-update-bot"
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
VERSION_FILE = os.path.join(DATA_DIR, ".version")

GITHUB_RAW_URL = "https://raw.githubusercontent.com/xymn2023/check-docker/main"
GITHUB_API_URL = "https://api.github.com/repos/xymn2023/check-docker/commits/main"

CHECK_INTERVAL = 3600  # 自动巡检间隔时间 (秒)

monitored_images = set()
scan_temp_state = {}
is_updating = False
last_check_time = "尚未执行"
TELEGRAM_BOT_TOKEN = ""
ALLOWED_CHAT_ID = ""


def load_config() -> bool:
    """读取本地 config.json 配置，不存在或无效时返回 False"""
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

    print("\n❌ 错误：未检测到有效配置！请运行 deploy.sh 部署管理脚本进行初始化。\n")
    return False


def load_tasks_from_disk():
    """读取已保存的镜像任务池"""
    global monitored_images
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    monitored_images = set(data)
                    logging.info(f"成功加载历史监控任务 {len(monitored_images)} 个。")
        except Exception as e:
            logging.error(f"读取 tasks.json 失败: {e}")


def save_tasks_to_disk():
    """写回任务池 JSON"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(monitored_images), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"保存 tasks.json 失败: {e}")


def run_cmd(cmd: str) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        return result.returncode, result.stdout.strip() + "\n" + result.stderr.strip()
    except Exception as e:
        return -1, str(e)


def check_docker_daemon() -> bool:
    code, _ = run_cmd("docker info")
    return code == 0


def is_valid_docker_image_name(image_name: str) -> bool:
    """
    【核心优化】校验 Docker 镜像名称是否属于合规的、可从云端仓库 Pull 的正规镜像名
    """
    if not image_name or not isinstance(image_name, str):
        return False

    image_name = image_name.strip()

    # 1. 过滤悬空镜像/无名镜像
    if "<none>" in image_name:
        return False

    # 2. 过滤纯 16 进制 Hash ID（如 1487abffa4fa 或 64位长ID）
    if re.fullmatch(r"[0-9a-fA-F]+", image_name):
        return False

    # 3. 匹配 Docker 官方标准镜像名称正则 (支持 域名/用户名/仓库名:标签)
    pattern = r"^(?:[a-zA-Z0-9.-]+(?::[0-9]+)?/)?(?:[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_.-]+(?::[a-zA-Z0-9_.-]+)?$"
    if not re.match(pattern, image_name):
        return False

    return True


def get_running_docker_images() -> list[str]:
    """
    【核心优化】实时获取当前服务器运行中容器的标准镜像列表
    排除匿名的 Image ID，自动补齐缺失的 :latest 标签
    """
    code, out = run_cmd("docker ps --format '{{.Image}}'")
    if code != 0 or not out.strip():
        return []

    valid_images = []
    for line in out.split("\n"):
        img = line.strip()
        if is_valid_docker_image_name(img):
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
    for img, checked in state.items():
        icon = "☑️" if checked else "⬜"
        keyboard.append([InlineKeyboardButton(f"{icon} {img}", callback_data=f"toggle:{img}")])

    keyboard.append([
        InlineKeyboardButton("☑️ 全部勾选", callback_data="select_all"),
        InlineKeyboardButton("⬜ 全部取消", callback_data="deselect_all")
    ])
    keyboard.append([InlineKeyboardButton("🚀 确认并保存监控任务", callback_data="save_tasks")])
    return InlineKeyboardMarkup(keyboard)


# ---------------- 🔒 绝对严格的鉴权函数 ----------------
async def auth_check(update: Update) -> bool:
    if not update or not update.effective_chat:
        return False

    incoming_chat_id = str(update.effective_chat.id)

    if incoming_chat_id != str(ALLOWED_CHAT_ID):
        logging.warning(f"⛔ 拦截未授权访问 ID: {incoming_chat_id}")
        if update.message:
            await update.message.reply_text("⛔ 无权使用此 Bot。")
        elif update.callback_query:
            await update.callback_query.answer("⛔ 无权使用此 Bot！", show_alert=True)
        return False

    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 欢迎指令（仅管理员可用）"""
    if not await auth_check(update): return
    await update.message.reply_text("👋 欢迎使用 Docker 镜像更新监控 Bot！")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scan 实时扫描当前容器的正规镜像"""
    if not await auth_check(update): return
    chat_id = update.effective_chat.id

    msg = await update.message.reply_text("🔎 正在实时检索服务器运行中的 Docker 容器镜像...", parse_mode="Markdown")

    if not check_docker_daemon():
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="❌ *Docker 引擎未启动或无法响应！*", parse_mode="Markdown")
        return

    current_running_images = get_running_docker_images()

    if not current_running_images:
        scan_temp_state[chat_id] = {}
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg.message_id,
            text="⚠️ 未检测到当前服务器上有任何合规的可检测 Docker 镜像。"
        )
        return

    new_scan_state = {}
    for img in current_running_images:
        new_scan_state[img] = (img in monitored_images)

    scan_temp_state[chat_id] = new_scan_state

    reply_markup = build_scan_keyboard(chat_id)
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg.message_id,
        text=f"🔍 *实时扫描到当前运行中 {len(current_running_images)} 个标准镜像*\n\n*(已自动剔除隐式 ID 与悬空镜像)*\n请勾选需要自动检测更新的镜像：",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

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
        save_tasks_to_disk()

        selected_text = "\n".join([f"• `{img}`" for img in monitored_images]) if monitored_images else "（当前未选择任何任务）"
        await query.edit_message_text(
            f"✅ *监控任务池已成功同步保存！*\n\n📌 当前已激活监控 ({len(monitored_images)} 个):\n{selected_text}\n\n⏱️ 系统将每隔 `{CHECK_INTERVAL}s` 自动巡检上述镜像。",
            parse_mode="Markdown"
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    docker_ok = "🟢 正常响应" if check_docker_daemon() else "🔴 异常或未启动"
    selected_text = "\n".join([f"• `{img}`" for img in monitored_images]) if monitored_images else "（未配置）"
    status_str = "🔄 更新中..." if is_updating else "💤 待命巡检"

    msg = (
        f"📊 *Docker 镜像自动更新服务状态*\n-----------------------------\n"
        f"⚙️ Docker 引擎: *{docker_ok}*\n⏱️ 上次检测: `{last_check_time}`\n📌 状态: *{status_str}*\n\n"
        f"🎯 *当前保存生效的监控任务 ({len(monitored_images)} 个):*\n{selected_text}\n\n"
        f"💡 /scan 重新扫描 | /check 立即检测 | /update 升级程序"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_update_self(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/update 脚本自我升级逻辑"""
    if not await auth_check(update): return

    msg = await update.message.reply_text("🔎 *[1/4] 正在检查 GitHub 上的最新发布代码...*", parse_mode="Markdown")

    code, api_out = run_cmd(f"curl -s -m 10 {GITHUB_API_URL}")
    if code != 0 or '"sha"' not in api_out:
        await context.bot.edit_message_text(chat_id=ALLOWED_CHAT_ID, message_id=msg.message_id, text="❌ 无法连接 GitHub API，请稍后重试。")
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
            text=f"🟢 *当前 Bot 程序已是最新版本！*\n📌 Commit: `{local_sha}`", parse_mode="Markdown"
        )
        return

    save_tasks_to_disk()

    await context.bot.edit_message_text(
        chat_id=ALLOWED_CHAT_ID, message_id=msg.message_id,
        text=f"🚀 *[2/4] 发现 GitHub 最新版本 (`{remote_sha}`)！*\n📥 正在平滑覆盖更新源码（配置与任务列表安全保留）...",
        parse_mode="Markdown"
    )

    code1, _ = run_cmd(f"curl -fsSL {GITHUB_RAW_URL}/autoupdate_bot.py -o {DATA_DIR}/autoupdate_bot.py")
    code2, _ = run_cmd(f"curl -fsSL {GITHUB_RAW_URL}/watchdog.py -o {DATA_DIR}/watchdog.py")

    if code1 != 0 or code2 != 0:
        await context.bot.edit_message_text(chat_id=ALLOWED_CHAT_ID, message_id=msg.message_id, text="❌ 源码拉取失败，更新中断。")
        return

    with open(VERSION_FILE, "w") as f: f.write(remote_sha)

    await context.bot.edit_message_text(
        chat_id=ALLOWED_CHAT_ID, message_id=msg.message_id,
        text="⚙️ *[3/4] 代码覆盖成功！*\n🔄 正在请求 Systemd 重启服务，请等待 5 秒...", parse_mode="Markdown"
    )

    os.environ["IS_SELF_UPGRADE"] = "true"
    await asyncio.sleep(2)
    run_cmd("systemctl restart docker-update-bot.service")


async def execute_update_check(context: ContextTypes.DEFAULT_TYPE, manual: bool = False):
    global is_updating, last_check_time
    if is_updating:
        if manual: await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="⚠️ 当前已有检测任务在进行中。")
        return

    if not check_docker_daemon():
        await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="🚨 Docker 引擎未响应，巡检暂停！")
        return

    if not monitored_images:
        if manual: await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="⚠️ 任务池为空，请先运行 /scan 重新扫描并勾选镜像。")
        return

    is_updating = True
    last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    images_list = list(monitored_images)
    total_count = len(images_list)
    progress_msg = None

    if manual:
        progress_msg = await context.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"🚀 *开始检测 {total_count} 个镜像的更新...*", parse_mode="Markdown"
        )

    results_summary = []
    try:
        for idx, img in enumerate(images_list, 1):
            if manual and progress_msg:
                try:
                    await context.bot.edit_message_text(
                        chat_id=ALLOWED_CHAT_ID, message_id=progress_msg.message_id,
                        text=f"🔎 *[ {idx}/{total_count} ] 正在检测：* `{img}`\n\n📥 对比远程 Registry 校验码...",
                        parse_mode="Markdown"
                    )
                except Exception: pass

            old_digest = get_image_digest(img)
            pull_code, _ = run_cmd(f"docker pull {img}")
            new_digest = get_image_digest(img)

            if pull_code == 0 and old_digest != new_digest:
                if manual and progress_msg:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=ALLOWED_CHAT_ID, message_id=progress_msg.message_id,
                            text=f"🔄 *[ {idx}/{total_count} ] 发现新版本！* `{img}`\n\n⚙️ 正在拉取镜像并重启关联容器...",
                            parse_mode="Markdown"
                        )
                    except Exception: pass

                code, container_names = run_cmd(f"docker ps -q --filter ancestor={img} | xargs -r docker inspect --format '{{{{.Name}}}}'")
                clean_names = [name.lstrip("/") for name in container_names.split("\n") if name.strip()]

                failed = False
                for c_name in clean_names:
                    r_code, _ = run_cmd(f"docker restart {c_name}")
                    if r_code != 0: failed = True

                res_str = f"✅ `{img}` -> 已更新并成功重启" if not failed else f"⚠️ `{img}` -> 已拉取更新，部分容器重启失败"
            else:
                res_str = f"🟢 `{img}` -> 已是最新版本"

            results_summary.append(res_str)
            await asyncio.sleep(0.5)

        summary_text = f"🏁 *{total_count} 个镜像检测完成！*\n\n" + "\n".join(results_summary)
        if manual and progress_msg:
            await context.bot.edit_message_text(chat_id=ALLOWED_CHAT_ID, message_id=progress_msg.message_id, text=summary_text, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"更新异常: {e}")
    finally:
        is_updating = False


async def cmd_manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    await execute_update_check(context, manual=True)


async def handle_unknown_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """拦截任何非授权用户的未定义消息或文本"""
    await auth_check(update)


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    await execute_update_check(context, manual=False)


async def post_init(application: Application):
    load_tasks_from_disk()

    await application.bot.set_my_commands([
        BotCommand("scan", "实时扫描当前容器镜像并管理任务"),
        BotCommand("check", "立即检测已有任务镜像更新"),
        BotCommand("update", "自动升级 Bot 程序自身"),
        BotCommand("status", "查看当前任务池与运行状态"),
    ])

    docker_status = "🟢 正常" if check_docker_daemon() else "🔴 未响应"
    current_ver = "未记录"
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, "r") as f: current_ver = f.read().strip()

    is_upgrade = os.getenv("IS_SELF_UPGRADE", "false") == "true"
    title = "🎉 *[4/4] Bot 自身升级完成！*" if is_upgrade else "🚀 *Bot 服务启动成功！*"

    if ALLOWED_CHAT_ID:
        await application.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"{title}\n------------------------------------\n"
                 f"📌 代码 Commit: `{current_ver}`\n"
                 f"🎯 已加载任务: *{len(monitored_images)} 个*\n"
                 f"⚙️ Docker 引擎: *{docker_status}*",
            parse_mode="Markdown"
        )


def main():
    if not load_config():
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # 注册常用命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("check", cmd_manual_check))
    app.add_handler(CommandHandler("update", cmd_update_self))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # 全局死锁兜底：任何陌生人发任何消息均触发 auth_check 静默/警告拦截
    app.add_handler(MessageHandler(filters.ALL, handle_unknown_messages))

    if app.job_queue:
        app.job_queue.run_repeating(scheduled_job, interval=CHECK_INTERVAL, first=30)

    app.run_polling()


if __name__ == "__main__":
    main()