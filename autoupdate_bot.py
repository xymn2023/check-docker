import os
import sys
import time
import json
import re
import subprocess
import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
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
FIRST_RUN_DELAY = 30   # 启动后首次巡检延迟 (秒)

monitored_images = set()
scan_temp_state = {}
is_updating = False
last_check_time = "尚未执行"
TELEGRAM_BOT_TOKEN = ""
ALLOWED_CHAT_ID = ""

# 定时巡检工作线程状态
patrol_worker_running = False
patrol_worker_task = None


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


def get_image_detail(image_name: str) -> dict:
    """
    获取镜像的详细版本信息：digest、创建日期、版本标签、OCI 标签等
    用于在更新通知中展示版本号和更新内容线索
    """
    detail = {
        "digest": "",
        "short_digest": "unknown",
        "created": "",
        "version": "",
        "source": "",
        "revision": "",
    }

    # 获取 digest
    detail["digest"] = get_image_digest(image_name)
    if "@" in detail["digest"]:
        detail["short_digest"] = detail["digest"].split("@")[-1][:19]

    # 使用 docker inspect 获取完整信息
    code, out = run_cmd(f"docker inspect --format '{{{{json .}}}}' {image_name}")
    if code != 0 or not out.strip():
        return detail

    try:
        # docker inspect --format 对每个结果输出一行 JSON，取第一行
        json_str = out.split("\n")[0].strip()
        info = json.loads(json_str)

        # 创建日期
        created = info.get("Created", "")
        if created:
            # 格式: 2024-01-15T10:30:00Z -> 2024-01-15
            detail["created"] = created.split("T")[0]

        # 从 Config.Labels 提取 OCI 标准版本信息
        labels = info.get("Config", {}).get("Labels", {}) or {}
        detail["version"] = (
            labels.get("org.opencontainers.image.version", "")
            or labels.get("version", "")
            or labels.get("org.label-schema.version", "")
        )
        detail["revision"] = (
            labels.get("org.opencontainers.image.revision", "")
            or labels.get("org.label-schema.vcs-ref", "")
        )
        detail["source"] = (
            labels.get("org.opencontainers.image.source", "")
            or labels.get("org.label-schema.url", "")
            or labels.get("org.opencontainers.image.url", "")
        )
    except Exception as e:
        logging.debug(f"解析镜像 {image_name} 详情失败: {e}")

    return detail


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
    patrol_str = "🟢 运行中" if patrol_worker_running else "🔴 未运行"

    msg = (
        f"📊 *Docker 镜像自动更新服务状态*\n-----------------------------\n"
        f"⚙️ Docker 引擎: *{docker_ok}*\n"
        f"⏰ 自动巡检: *{patrol_str}*\n"
        f"🕒 巡检间隔: `{CHECK_INTERVAL}s`\n"
        f"⏱️ 上次检测: `{last_check_time}`\n"
        f"📌 状态: *{status_str}*\n\n"
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
    """
    核心更新检测逻辑
    - manual=True: 手动触发，显示逐条进度，最后发送完整汇总
    - manual=False: 自动巡检，无更新时静默跳过；发现更新时立即通知并发送汇总
    """
    global is_updating, last_check_time
    if is_updating:
        if manual:
            await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="⚠️ 当前已有检测任务在进行中。")
        return

    if not check_docker_daemon():
        await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="🚨 Docker 引擎未响应，巡检暂停！")
        return

    if not monitored_images:
        if manual:
            await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="⚠️ 任务池为空，请先运行 /scan 重新扫描并勾选镜像。")
        return

    is_updating = True
    last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    images_list = list(monitored_images)
    total_count = len(images_list)
    progress_msg = None

    # 记录本次巡检中发现更新的镜像（用于自动模式汇总通知）
    updated_items = []

    if manual:
        progress_msg = await context.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"🚀 *开始检测 {total_count} 个镜像的更新...*", parse_mode="Markdown"
        )

    results_summary = []
    try:
        for idx, img in enumerate(images_list, 1):
            # 手动模式：更新进度消息
            if manual and progress_msg:
                try:
                    await context.bot.edit_message_text(
                        chat_id=ALLOWED_CHAT_ID, message_id=progress_msg.message_id,
                        text=f"🔎 *[ {idx}/{total_count} ] 正在检测：* `{img}`\n\n📥 对比远程 Registry 校验码...",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

            # 记录更新前的镜像信息
            old_digest = get_image_digest(img)
            old_detail = get_image_detail(img)

            # 拉取镜像（如果远程有新版本则会实际下载）
            pull_code, pull_out = run_cmd(f"docker pull {img}")

            # 获取更新后的镜像信息
            new_digest = get_image_digest(img)
            new_detail = get_image_detail(img)

            has_update = pull_code == 0 and old_digest != new_digest and new_digest != ""

            if has_update:
                # 构造版本信息文本
                version_info = ""
                if new_detail["version"]:
                    version_info += f"\n🏷️ 版本标签: `{new_detail['version']}`"
                if new_detail["created"]:
                    version_info += f"\n📅 构建日期: `{new_detail['created']}`"
                if new_detail["revision"]:
                    version_info += f"\n🔖 Git Commit: `{new_detail['revision'][:12]}`"
                if new_detail["source"]:
                    version_info += f"\n🔗 源码地址: {new_detail['source']}"

                digest_info = f"`{old_detail['short_digest']}...` → `{new_detail['short_digest']}...`"

                # 手动模式：编辑进度消息
                if manual and progress_msg:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=ALLOWED_CHAT_ID, message_id=progress_msg.message_id,
                            text=f"🔄 *[ {idx}/{total_count} ] 发现新版本！* `{img}`\n\n"
                                 f"📦 Digest 变更: {digest_info}"
                                 f"{version_info}\n\n"
                                 f"⚙️ 正在重启关联容器...",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                # 自动模式：立即发送更新通知
                else:
                    await context.bot.send_message(
                        chat_id=ALLOWED_CHAT_ID,
                        text=f"🔄 *[自动巡检] 发现镜像更新！* `{img}`\n\n"
                             f"📦 Digest 变更: {digest_info}"
                             f"{version_info}\n\n"
                             f"⚙️ 正在重启关联容器...",
                        parse_mode="Markdown"
                    )

                # 查找使用该镜像的运行中容器并重启
                code, container_names = run_cmd(
                    f"docker ps -q --filter ancestor={img} | xargs -r docker inspect --format '{{{{.Name}}}}'"
                )
                clean_names = [name.lstrip("/") for name in container_names.split("\n") if name.strip()]

                failed = False
                restart_details = []
                for c_name in clean_names:
                    r_code, _ = run_cmd(f"docker restart {c_name}")
                    if r_code != 0:
                        failed = True
                        restart_details.append(f"  ❌ `{c_name}` 重启失败")
                    else:
                        restart_details.append(f"  ✅ `{c_name}` 已重启")

                restart_text = "\n".join(restart_details) if restart_details else "  （无关联运行中容器）"

                if not failed:
                    res_str = f"✅ `{img}` -> 已更新并成功重启"
                else:
                    res_str = f"⚠️ `{img}` -> 已拉取更新，部分容器重启失败"

                updated_items.append({
                    "image": img,
                    "old_digest": old_detail["short_digest"],
                    "new_digest": new_detail["short_digest"],
                    "version": new_detail["version"],
                    "created": new_detail["created"],
                    "restart_text": restart_text,
                    "failed": failed,
                })

                # 自动模式：单镜像更新完成通知
                if not manual:
                    icon = "✅" if not failed else "⚠️"
                    await context.bot.send_message(
                        chat_id=ALLOWED_CHAT_ID,
                        text=f"{icon} *[自动巡检] 镜像更新完成！* `{img}`\n\n"
                             f"🔄 容器重启状态:\n{restart_text}",
                        parse_mode="Markdown"
                    )
            else:
                if pull_code != 0:
                    res_str = f"❌ `{img}` -> 拉取失败"
                else:
                    res_str = f"🟢 `{img}` -> 已是最新版本"

            results_summary.append(res_str)
            await asyncio.sleep(0.5)

        # 手动模式：发送完整汇总（编辑进度消息）
        if manual and progress_msg:
            summary_text = f"🏁 *{total_count} 个镜像检测完成！*\n⏱️ 时间: `{last_check_time}`\n\n" + "\n".join(results_summary)
            await context.bot.edit_message_text(
                chat_id=ALLOWED_CHAT_ID,
                message_id=progress_msg.message_id,
                text=summary_text,
                parse_mode="Markdown"
            )

        # 自动模式：如果有更新，发送汇总通知；无更新则静默跳过
        if not manual and updated_items:
            auto_summary = (
                f"🏁 *[自动巡检] 本次巡检完成！*\n"
                f"⏱️ 检测时间: `{last_check_time}`\n"
                f"📊 共检测 {total_count} 个镜像，{len(updated_items)} 个有更新\n\n"
            )
            for item in updated_items:
                icon = "✅" if not item["failed"] else "⚠️"
                ver = f" (v{item['version']})" if item["version"] else ""
                auto_summary += (
                    f"{icon} *{item['image']}*{ver}\n"
                    f"  📦 `{item['old_digest']}...` → `{item['new_digest']}...`\n"
                    f"{item['restart_text']}\n\n"
                )
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=auto_summary,
                parse_mode="Markdown"
            )

    except Exception as e:
        logging.error(f"更新异常: {e}")
        if manual and progress_msg:
            try:
                await context.bot.edit_message_text(
                    chat_id=ALLOWED_CHAT_ID,
                    message_id=progress_msg.message_id,
                    text=f"❌ *检测过程中发生异常！*\n`{str(e)[:300]}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        elif not manual:
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"🚨 *[自动巡检] 检测过程中发生异常！*\n`{str(e)[:300]}`",
                parse_mode="Markdown"
            )
    finally:
        is_updating = False


async def cmd_manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    await execute_update_check(context, manual=True)


async def handle_unknown_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """拦截任何非授权用户的未定义消息或文本"""
    await auth_check(update)


# ==================== 原生 asyncio 定时巡检（不依赖 JobQueue/APScheduler） ====================

async def scheduled_patrol_worker(bot: "Bot"):
    """
    后台定时巡检工作协程
    使用 asyncio.sleep 实现精确定时，完全不依赖 python-telegram-bot 的 JobQueue 组件，
    因此无需安装 APScheduler，避免因依赖缺失导致自动巡检静默失效。
    """
    global patrol_worker_running
    patrol_worker_running = True
    logging.info(f"✅ 定时巡检协程已启动，启动后 {FIRST_RUN_DELAY} 秒执行首次巡检，之后每隔 {CHECK_INTERVAL} 秒巡检一次。")

    # 构造一个简易 context 对象，execute_update_check 只需要 context.bot
    fake_context = SimpleNamespace(bot=bot)

    try:
        # 首次延迟
        await asyncio.sleep(FIRST_RUN_DELAY)

        while True:
            try:
                logging.info("⏰ [定时巡检] 触发自动检测...")
                await execute_update_check(fake_context, manual=False)
            except asyncio.CancelledError:
                logging.info("🛑 定时巡检协程收到取消信号，正在退出...")
                break
            except Exception as e:
                logging.error(f"❌ 定时巡检发生未捕获异常: {e}")
                try:
                    await bot.send_message(
                        chat_id=ALLOWED_CHAT_ID,
                        text=f"🚨 *[定时巡检] 发生异常！*\n`{str(e)[:300]}`",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

            # 等待下一次巡检间隔
            await asyncio.sleep(CHECK_INTERVAL)

    except asyncio.CancelledError:
        logging.info("🛑 定时巡检协程已取消。")
    finally:
        patrol_worker_running = False
        logging.info("🛑 定时巡检协程已停止。")


async def post_init(application: Application):
    global patrol_worker_task
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

    # 启动原生 asyncio 定时巡检协程（不依赖 JobQueue / APScheduler）
    patrol_worker_task = application.create_task(scheduled_patrol_worker(application.bot))

    if ALLOWED_CHAT_ID:
        await application.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"{title}\n------------------------------------\n"
                 f"📌 代码 Commit: `{current_ver}`\n"
                 f"🎯 已加载任务: *{len(monitored_images)} 个*\n"
                 f"⚙️ Docker 引擎: *{docker_status}*\n"
                 f"⏰ 自动巡检: *🟢 已启用（asyncio 原生定时）*\n"
                 f"🕒 巡检间隔: `{CHECK_INTERVAL}s`\n"
                 f"⏱️ 首次巡检: 启动后 `{FIRST_RUN_DELAY}s`",
            parse_mode="Markdown"
        )


async def post_shutdown(application: Application):
    """应用关闭时清理定时巡检协程"""
    global patrol_worker_task
    if patrol_worker_task and not patrol_worker_task.done():
        patrol_worker_task.cancel()
        try:
            await patrol_worker_task
        except asyncio.CancelledError:
            pass


def main():
    if not load_config():
        sys.exit(1)

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # 注册常用命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("check", cmd_manual_check))
    app.add_handler(CommandHandler("update", cmd_update_self))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # 全局死锁兜底：任何陌生人发任何消息均触发 auth_check 静默/警告拦截
    app.add_handler(MessageHandler(filters.ALL, handle_unknown_messages))

    print("🚀 Telegram Docker Update Bot 已启动，正在监听命令...")
    print(f"⏰ 自动巡检将在启动后 {FIRST_RUN_DELAY} 秒开始，间隔 {CHECK_INTERVAL} 秒（使用 asyncio 原生定时，无需 APScheduler）")
    app.run_polling()


if __name__ == "__main__":
    main()
