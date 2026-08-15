# 🐳 Docker 镜像自动更新 Telegram Bot

一个基于 Python 的 Docker 镜像自动检测、无缝更新与重启通知工具。通过 Telegram Bot 交互式勾选要监控的镜像，定时巡检远程仓库，发现新版本自动拉取并重启关联容器，全程消息通知。

## ✨ 功能特性

- 🔍 **实时扫描**：`/scan` 一键扫描当前运行中容器的镜像（自动剔除悬空镜像与匿名 ID），交互式勾选加入监控任务池
- ⏰ **定时巡检**：基于 asyncio 原生定时（无需 APScheduler），默认每小时自动检测一次镜像更新
- 🔄 **自动更新**：通过对比镜像 Digest 判断是否有新版本，自动 `docker pull` 并重启关联容器
- 📋 **版本详情通知**：更新通知中展示 Digest 变化、版本标签、构建日期、Git Commit、源码地址（读取镜像 OCI 标准标签）
- 🛡️ **守护自愈**：内置 Watchdog 守护进程，主程序意外退出时 15 秒内自动拉起并发送 Telegram 告警
- ⬆️ **自我升级**：`/update` 检测 GitHub 最新 Commit，平滑覆盖更新自身代码（配置与任务池安全保留）
- 🔒 **权限隔离**：仅响应指定 Chat ID 的指令，拦截任何未授权访问
- 📦 **一键部署**：交互式安装脚本，自动创建虚拟环境、配置 systemd 开机自启

## 🚀 快速一键安装

在你的 Linux 服务器上以 root 运行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/xymn2023/check-docker/main/deploy.sh)
```

安装过程会依次要求输入：

1. **TELEGRAM_BOT_TOKEN** — 从 [@BotFather](https://t.me/BotFather) 创建 Bot 获取
2. **ALLOWED_CHAT_ID** — 你的 Telegram Chat ID（可从 [@userinfobot](https://t.me/userinfobot) 获取），Bot 只响应此 ID 的指令

安装完成后服务即自动启动并开机自启。

> 💡 提示：获取 Chat ID 前，请先向你的 Bot 发送一条消息（如 `/start`），否则 Bot 无法主动向你推送通知。

## 🤖 Bot 命令列表

| 命令 | 说明 |
|------|------|
| `/scan` | 实时扫描当前运行中的容器镜像，弹出勾选面板管理监控任务池 |
| `/check` | 立即手动检测任务池中所有镜像的更新（带逐条进度与汇总报告） |
| `/status` | 查看运行状态、Docker 引擎状态、任务池列表与上次巡检时间 |
| `/update` | 检查并自动升级 Bot 程序自身（对比 GitHub 最新 Commit） |
| `/start` | 欢迎消息 |

**扫描面板按钮**：

- 点击镜像行 ☑️/⬜ 切换勾选状态
- `全部勾选` / `全部取消` 批量操作
- `确认并保存监控任务` 写入任务池并立即生效

## ⚙️ 运行机制

1. **更新检测**：对任务池中每个镜像，先记录本地 Digest，执行 `docker pull`，再对比拉取后的 Digest，不一致即判定为新版本
2. **自动重启**：通过 `docker ps --filter ancestor=<镜像>` 查找使用该镜像的运行中容器，逐个 `docker restart`
3. **通知推送**：手动模式编辑进度消息并最终汇总；自动巡检模式无更新时静默，有更新时即时通知 + 巡检汇总
4. **定时巡检**：主程序启动后 30 秒执行首次巡检，此后每 3600 秒（1 小时）一次
5. **守护自愈**：systemd 托管 `watchdog.py`，Watchdog 每 15 秒检查主程序进程，异常退出时自动拉起并推送告警

## 🛠️ 服务器端管理

安装后再次运行部署脚本即可进入交互式管理菜单：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/xymn2023/check-docker/main/deploy.sh)
```

菜单功能：

| 选项 | 功能 |
|------|------|
| 1 | 查看运行状态 / 实时日志（journalctl） |
| 2 | 更新 Bot 程序代码（含重建 venv 依赖与服务配置） |
| 3 | 修改 Token / Chat ID 配置 |
| 4 | 重启 Bot 服务 |
| 5 | 停止 Bot 服务 |
| 6 | 彻底卸载程序（清除所有文件与配置） |
| 7 | 修复安装（重建 venv / 依赖 / service 文件） |

也可以直接使用 systemd 命令：

```bash
systemctl status docker-update-bot      # 查看服务状态
journalctl -u docker-update-bot -f      # 实时查看日志
systemctl restart docker-update-bot     # 重启服务
```

## 📁 文件与目录说明

| 路径 | 说明 |
|------|------|
| `/opt/docker-update-bot/autoupdate_bot.py` | 主程序（Telegram Bot + 巡检逻辑） |
| `/opt/docker-update-bot/watchdog.py` | 守护进程（监控并自动拉起主程序） |
| `/opt/docker-update-bot/config.json` | Token 与 Chat ID 配置 |
| `/opt/docker-update-bot/tasks.json` | 已保存的镜像监控任务池 |
| `/opt/docker-update-bot/.version` | 当前程序版本（Git Commit SHA） |
| `/etc/systemd/system/docker-update-bot.service` | systemd 服务文件 |

## 🔧 高级配置

调整巡检间隔：编辑 `autoupdate_bot.py` 顶部的常量后重启服务：

```python
CHECK_INTERVAL = 3600   # 自动巡检间隔（秒）
FIRST_RUN_DELAY = 30    # 启动后首次巡检延迟（秒）
```

```bash
systemctl restart docker-update-bot
```

## 🗑️ 卸载

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/xymn2023/check-docker/main/uninstall.sh)
```

> ⚠️ 卸载将彻底停止服务，并清空所有已保存的 Token、Chat ID 以及镜像任务池，操作不可恢复。

## ❓ 常见问题

**Q：Bot 不回复消息？**
确认 Chat ID 正确，且已先向 Bot 发送过消息；运行 `journalctl -u docker-update-bot -n 50` 查看日志。

**Q：自动巡检没有生效？**
发送 `/status` 查看「自动巡检」是否显示 🟢 运行中；若显示 🔴，重启服务即可。

**Q：某些镜像检测不到更新？**
工具依赖镜像的 RepoDigest 做对比，本地 `docker build` 自建且从未 push 过的镜像没有 RepoDigest，无法纳入检测。

**Q：如何修改被监控的镜像列表？**
随时发送 `/scan` 重新扫描勾选，保存后立即生效，无需重启。

## 📄 依赖

- Linux 服务器（root 权限）+ Docker 引擎 + systemd
- Python 3（安装脚本会自动创建虚拟环境）
- `python-telegram-bot >= 20.0`、`requests >= 2.28.0`
