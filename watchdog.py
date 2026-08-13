import os
import sys
import time
import subprocess
import requests
from datetime import datetime

# 优先从 Systemd 注入的环境变量中读取
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "")
MAIN_SCRIPT = "autoupdate_bot.py"


def send_tg_msg(text: str):
    """守护进程直接调用 Telegram API 发送通知"""
    if not BOT_TOKEN or not CHAT_ID:
        print("[Watchdog] 未配置 TELEGRAM_BOT_TOKEN 或 ALLOWED_CHAT_ID，跳过发送消息。")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[Watchdog] 发送 TG 消息失败: {e}")


def is_main_script_running() -> bool:
    """检查主程序进程是否存在"""
    cmd = f"ps aux | grep '{MAIN_SCRIPT}' | grep -v 'grep' | grep -v 'watchdog.py'"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return res.returncode == 0 and len(res.stdout.strip()) > 0


def start_main_script():
    """使用当前虚拟环境下的 Python 解释器启动主程序"""
    env = dict(os.environ, IS_RESTART_EVENT="true")
    # sys.executable 会自动匹配 venv 环境下的 bin/python 绝对路径
    process = subprocess.Popen([sys.executable, MAIN_SCRIPT], env=env)
    return process


def main():
    print("🛡️ Watchdog 守护进程已启动，开始实时监控主程序...")

    while True:
        if not is_main_script_running():
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now_str}] ⚠️ 告警：检测到主程序停止运行！正在尝试重启...")

            # 1. 发送告警消息
            send_tg_msg(
                f"🚨 *【服务器告警】Docker 监控主程序意外停止！*\n"
                f"⏱️ 发生时间: `{now_str}`\n"
                f"⚙️ 守护程序正在尝试自动重启..."
            )

            # 2. 拉起主程序
            try:
                start_main_script()
                time.sleep(10)  # 等待 10 秒校验进程存续

                if is_main_script_running():
                    print(f"[{datetime.now()}] ✅ 主程序自动重启成功。")
                else:
                    raise RuntimeError("进程拉起后未能持续运行")
            except Exception as e:
                err_msg = str(e)
                print(f"[{datetime.now()}] ❌ 主程序重启失败: {err_msg}")
                send_tg_msg(
                    f"❌ *【严重错误】Docker 监控主程序自动重启失败！*\n"
                    f"⚠️ 错误原因: `{err_msg}`\n"
                    f"🛠️ 请登录服务器手动检查日志。"
                )

        # 每 15 秒检测一次主程序状态
        time.sleep(15)


if __name__ == "__main__":
    main()