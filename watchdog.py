import os
import sys
import time
import subprocess
import requests
from datetime import datetime

# ==================== 路径与环境自修复 ====================

# 获取脚本所在目录（watchdog.py 所在目录，即 /opt/docker-update-bot）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(SCRIPT_DIR, "venv", "bin", "python")
MAIN_SCRIPT = os.path.join(SCRIPT_DIR, "autoupdate_bot.py")
MAIN_SCRIPT_NAME = "autoupdate_bot.py"


def ensure_venv_python():
    """
    如果当前不是用虚拟环境 Python 运行的，但虚拟环境存在，
    则自动用 venv Python 重新启动自身。
    这确保即使 systemd service 文件误指向系统 Python，也能自动修正。
    """
    current_python = sys.executable
    venv_python_real = os.path.realpath(VENV_PYTHON) if os.path.exists(VENV_PYTHON) else ""
    current_python_real = os.path.realpath(current_python)

    if venv_python_real and current_python_real != venv_python_real:
        print(f"[Watchdog] 当前 Python: {current_python}")
        print(f"[Watchdog] 检测到虚拟环境 Python: {VENV_PYTHON}")
        print(f"[Watchdog] 正在自动切换到虚拟环境 Python 重新启动...")
        os.execv(VENV_PYTHON, [VENV_PYTHON] + sys.argv)


# 优先从 Systemd 注入的环境变量中读取
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "")


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
    # 使用绝对路径匹配，避免误判
    cmd = f"ps aux | grep '{MAIN_SCRIPT_NAME}' | grep -v 'grep' | grep -v 'watchdog.py'"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return res.returncode == 0 and len(res.stdout.strip()) > 0


def start_main_script():
    """使用当前 Python 解释器（已确保是 venv Python）启动主程序"""
    env = dict(os.environ, IS_RESTART_EVENT="true")
    # sys.executable 在 ensure_venv_python() 之后已经是 venv Python
    process = subprocess.Popen([sys.executable, MAIN_SCRIPT], cwd=SCRIPT_DIR, env=env)
    return process


def main():
    # 启动前自修复：确保使用 venv Python
    ensure_venv_python()

    print(f"🛡️ Watchdog 守护进程已启动 (Python: {sys.executable})")
    print(f"📂 工作目录: {SCRIPT_DIR}")
    print(f"📜 主程序: {MAIN_SCRIPT}")
    print("开始实时监控主程序...")

    # 启动时立即拉起主程序
    if not is_main_script_running():
        print(f"[{datetime.now()}] 主程序未运行，正在启动...")
        try:
            start_main_script()
            time.sleep(10)
        except Exception as e:
            print(f"[{datetime.now()}] ❌ 初始启动主程序失败: {e}")
            send_tg_msg(
                f"🚨 *【服务器告警】Docker 监控主程序启动失败！*\n"
                f"⚠️ 错误原因: `{str(e)}`\n"
                f"🛠️ 请登录服务器手动检查。"
            )

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
                    send_tg_msg(
                        f"✅ *【恢复通知】Docker 监控主程序已自动重启成功！*\n"
                        f"⏱️ 恢复时间: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
                    )
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
