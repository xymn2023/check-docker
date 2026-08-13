import os
import sys
import time
import subprocess
import asyncio
from datetime import datetime
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== 配置区域 ====================
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # 替换为你的 Bot Token
ALLOWED_CHAT_ID = "YOUR_CHAT_ID_HERE"        # 替换为你的 Chat ID (安全限制：只响应你的指令)

# 检测间隔（秒），如 3600 秒 = 1 小时
CHECK_INTERVAL = 3600

# 项目路径 (Docker Compose 所在目录)
PROJECT_DIR = "/path/to/your/project"
# ==================================================

# 全局变量，记录上次检查时间和状态
last_check_time = "尚未执行"
is_checking = False


def run_cmd(cmd: str, cwd: str = None) -> tuple[int, str]:
    """执行 Shell 命令并返回 code 和 output"""
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, cwd=cwd
    )
    return result.returncode, result.stdout.strip() + "\n" + result.stderr.strip()


async def check_and_update(context: ContextTypes.DEFAULT_TYPE, chat_id: str, manual: bool = False):
    """核心更新检测逻辑"""
    global is_checking, last_check_time
    if is_checking:
        if manual:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ 当前已有更新任务在运行中，请勿重复操作。")
        return

    is_checking = True
    last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # 如果是手动触发，先发一条回执
        if manual:
            init_msg = await context.bot.send_message(chat_id=chat_id, text="🔍 *收到指令，正在检查 Docker 镜像更新...*", parse_mode="Markdown")

        # 执行 pull 检查
        pull_code, pull_out = run_cmd("docker compose pull", cwd=PROJECT_DIR)

        if "Downloaded newer image" in pull_out or "Pulled" in pull_out:
            # 发现新镜像
            msg_text = (
                f"🔍 *[Docker Compose] 检测到镜像更新！*\n"
                f"📁 项目: `{PROJECT_DIR}`\n"
                f"⏳ 正在拉取新镜像..."
            )
            if manual:
                msg = await context.bot.edit_message_text(chat_id=chat_id, message_id=init_msg.message_id, text=msg_text, parse_mode="Markdown")
            else:
                msg = await context.bot.send_message(chat_id=chat_id, text=msg_text, parse_mode="Markdown")

            await asyncio.sleep(1)

            # 更新提示消息
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg.message_id,
                text=f"🔄 *[Docker Compose] 镜像更新成功！*\n⚙️ 正在重新构建并重启容器...",
                parse_mode="Markdown"
            )

            # 执行 up -d
            up_code, up_out = run_cmd("docker compose up -d", cwd=PROJECT_DIR)

            if up_code == 0:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    text=f"✅ *[Docker Compose] 服务更新并重启成功！*\n📁 项目: `{PROJECT_DIR}`\n⏱️ 完成时间: `{now_str}`",
                    parse_mode="Markdown"
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    text=f"❌ *[Docker Compose] 重启服务失败！*\n```\n{up_out[:300]}\n```",
                    parse_mode="Markdown"
                )
        else:
            if manual:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=init_msg.message_id,
                    text=f"👌 *所有镜像均已是最新版本，无需更新。*\n⏱️ 检查时间: `{last_check_time}`",
                    parse_mode="Markdown"
                )
    finally:
        is_checking = False


# ==================== 快捷命令处理逻辑 ====================

async def auth_check(update: Update) -> bool:
    """权限校验，防止其他人操作你的 Bot"""
    if str(update.effective_chat.id) != str(ALLOWED_CHAT_ID):
        await update.message.reply_text("⛔ 无权使用此 Bot。")
        return False
    return True


async def cmd_start_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 和 /help 命令响应"""
    if not await auth_check(update): return
    help_text = (
        "🤖 *Docker 自动化更新 Bot* 命令列表：\n\n"
        "🟢 /check - 立即手动检测并更新镜像\n"
        "📊 /status - 查看当前运行状态与上次检测时间\n"
        "❓ /help - 显示此帮助菜单"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/check 手动触发检测指令"""
    if not await auth_check(update): return
    await check_and_update(context, chat_id=update.effective_chat.id, manual=True)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status 查看状态指令"""
    if not await auth_check(update): return
    status_str = "🔄 正在检测中..." if is_checking else "💤 待命/定时巡检中"
    msg = (
        f"📊 *Bot 运行状态报告*\n"
        f"-------------------\n"
        f"⚙️ 监控路径: `{PROJECT_DIR}`\n"
        f"⏱️ 上次检测时间: `{last_check_time}`\n"
        f"🕒 定时检测间隔: `{CHECK_INTERVAL}s`\n"
        f"📌 当前状态: *{status_str}*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    """后台定时巡检任务"""
    await check_and_update(context, chat_id=ALLOWED_CHAT_ID, manual=False)


async def post_init(application: Application):
    """Bot 初始化时设置快捷命令菜单列表"""
    commands = [
        BotCommand("check", "立即手动检测并更新镜像"),
        BotCommand("status", "查看当前监控服务状态"),
        BotCommand("help", "查看帮助信息"),
    ]
    await application.bot.set_my_commands(commands)


def main():
    # 构建 Telegram Application
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # 注册命令 Handler
    app.add_handler(CommandHandler(["start", "help"], cmd_start_help))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("status", cmd_status))

    # 配置定时巡检任务 (JobQueue)
    if app.job_queue:
        app.job_queue.run_repeating(scheduled_job, interval=CHECK_INTERVAL, first=10)

    print("🚀 Telegram Docker Update Bot 已启动，正在监听命令与定时任务...")
    app.run_polling()


if __name__ == "__main__":
    main()