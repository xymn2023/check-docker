# check-docker v2.0.0 使用说明

Telegram Docker 镜像监控与 Compose 更新工具。此精简交付包仅保留 9 个文件：主程序、更新引擎、配置管理、安装脚本、卸载脚本、依赖清单、配置示例、本说明与完整性校验清单。

基于 xymn2023/check-docker 提交 a2a382512c2ef9c73797215edf51b15c50c96b69 二次开发。上游未提供 LICENSE，本包不替原作者授予许可。

普通容器仅拉取和通知；显式配置的 Compose 单副本服务支持自动重建、验证及失败时尝试恢复旧镜像。

验证状态：更新逻辑通过 27 项自动化测试；精简包已检查语法和文件完整性。尚未完成真实 Docker、Telegram 与 systemd 联调。

## 1. 适用范围

本教程在 Linux 服务器操作。Windows 可以用于下载 ZIP，并通过 SFTP 工具将文件上传服务器。

要求：

- Python 3.10 或更新版本，venv 和 pip。
- systemd 管理服务。
- Docker Engine 已安装并运行；Compose v2 插件。
- Compose 支持 `config --format json`、`up --no-deps --no-build --pull never`。
- 能访问 Telegram API、Python 包源、你的镜像仓库。
- 有 root 权限；Docker 控制本身具有很高的主机权限。

先检查：

```bash
python3 --version
docker info
docker compose version
systemctl --version
```

Debian/Ubuntu 缺少 Python 组件时：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip unzip
```

若发行版自带 Python 小于 3.10，请先升级受支持的 Python 环境。Docker/Compose 的安装请参照官方对应系统说明：
https://docs.docker.com/engine/install/

本安装器不自动更换已有 Docker，也不修改镜像加速器、代理或防火墙。

## 2. 新安装

### 2.1 准备 Telegram

1. 向 @BotFather 创建 Bot，取得 Token。
2. 获取自己的数字 User ID。私聊时 Chat ID 通常就是你的 User ID。
3. 先打开新 Bot 并发送 `/start`。
4. 如在群组使用，Chat ID 填群组 ID（通常为负数），允许用户列表填你的个人数字 ID；不能填群组 ID 代替用户 ID。

### 2.2 上传并解压

将本次提供的 ZIP 上传到服务器，例如 `/root/packages`。文件名以实际下载文件为准：

```bash
cd /root/packages
unzip check-docker-v2.0.0.zip
cd check-docker-v2.0.0
sudo bash deploy.sh
```

菜单选择 1。安装过程将：

1. 检查运行环境和包内 SHA256SUMS。
2. 建立独立版本目录及虚拟环境，安装锁定版本的依赖。
3. 隐藏输入 Token，输入 Chat ID 和允许的用户 ID。
4. 安装 systemd 服务，等待 Bot 初始化和轮询启动，最长 60 秒。
5. 如果新版本未就绪，尝试恢复原服务；首次安装则保留文件供修复。

校验和用于检查损坏和不完整文件，不证明下载者身份。只安装你审核或信任的包。

### 2.3 选择监控任务

向 Bot 发送 `/scan`：

- 普通容器显示“仅通知”。
- 明确配置的 Compose 服务显示 `notify` 或 `auto`。
- 历史任务也保留，不会因为容器暂时停止而被静默删除。
- 用上一页/下一页浏览；全选影响所有页面。
- 点“保存”后生效；巡检已开始时，改动从下一轮生效。
- 新扫描使旧面板失效；重启后需要重新扫描。

发 `/check`。检查会实际下载镜像，所以 notify 不是“只查询远程元数据”。它不会重建服务，但本地镜像标签可能变化。

## 3. 从原版迁移

使用同一个 `/opt/docker-update-bot` 数据目录，运行本版 `deploy.sh` 选择 1。

- 原 `config.json` 继续使用，自动提供新字段默认值。
- `config.before-v2.json` 保存首次迁移前的配置备份。
- 原 `tasks.json` 保留，不会覆盖。
- 新任务文件是 `tasks-v2.json`，旧镜像名转换成 `legacy:镜像名` 待映射条目。
- 打开 `/scan` 勾选新的容器/Compose 目标，取消已经替代的 legacy 条目，再保存。
- 旧 Watchdog 不再运行，systemd 直接启动新主程序。
- 若旧配置使用群组 ID，先补 `allowed_user_ids`，否则配置验证会拒绝安装。

旧脚本、旧 venv 会保留在原目录，但不再作为新服务入口。不要在新服务运行时手动启动它们。

## 4. 配置 Compose 服务

自动更新只用于你显式配置的单副本服务。没有配置文件路径的普通容器不会被猜测重建。

编辑 `/opt/docker-update-bot/config.json`。以下是完整示例；路径、项目名、服务名、Token 和 ID 均要替换：

```json
{
  "bot_token": "你的Token",
  "chat_id": 123456789,
  "allowed_user_ids": [123456789],
  "check_interval": 36000,
  "first_run_delay": 60,
  "pull_timeout": 1800,
  "command_timeout": 180,
  "health_timeout": 120,
  "stability_seconds": 15,
  "notify_unchanged": false,
  "compose_targets": {
    "web": {
      "project": "myapp",
      "service": "web",
      "directory": "/srv/myapp",
      "files": ["/srv/myapp/compose.yaml"],
      "env_files": ["/srv/myapp/.env"],
      "mode": "notify",
      "allow_no_healthcheck": false
    }
  }
}
```

目标 ID `web` 是 Bot 使用的短标识；`project` 是实际 Compose 项目名，`service` 是配置中 services 下的服务名，不一定等于容器名。

可以用下面命令查看实际标签，替换容器名称：

```bash
docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' 容器名称
docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' 容器名称
```

如果没有自定义 `.env` 文件，删除 `env_files` 项；所有提供的路径必须存在且为绝对路径。多份 Compose 文件按原部署顺序放入 `files`。不要依赖仅存在于交互式终端的环境变量；把需要的插值变量放入明确的环境文件。

配置完成后验证并重启：

```bash
sudo /opt/docker-update-bot/current/venv/bin/python /opt/docker-update-bot/current/admin.py --validate
sudo systemctl restart docker-update-bot
```

随后 `/scan`，勾选 `Compose myapp/web [notify]`，保存，先运行 `/check`。

确认匹配正确、健康检查可靠、数据已经备份后，将 `mode` 改为 `auto`，重新验证并重启。自动模式是服务器配置授权，Telegram 勾选只控制该目标是否参与巡检。

### 4.1 健康检查

默认要求正在运行的服务具备 Docker HEALTHCHECK。健康检查命令必须适合你的应用，并且镜像内部必须具备命令所需工具。

如果确实没有健康检查，可以显式设置：

```json
"allow_no_healthcheck": true
```

此时只验证正确镜像和持续运行时间，不证明网页、数据库或 API 业务健康。`health_timeout` 必须大于 `stability_seconds`。

### 4.2 更新时实际发生什么

1. 确认项目/服务只有一个正在运行的容器。
2. 解析原 Compose 配置，取得期望镜像标签。
3. 根据运行镜像的平台执行 pull。
4. 比较运行镜像 ID 与新镜像 ID；已有人提前 pull 也能识别。
5. 再次检查容器身份和配置是否发生变化。
6. 保留旧镜像标签，给新镜像创建固定本地标签，写入事务和配置快照。
7. 用快照执行指定服务的 `compose up -d --no-deps --no-build --pull never`。
8. 检查镜像 ID、运行状态、重启次数/启动时间稳定性和健康检查。
9. 新版本失败时，尝试以旧镜像重建并验证。

Compose 的卷配置会保留，程序不使用 `down -v`、不删除数据卷、不自动清理镜像。重建期间有停机窗口。容器可写层不属于持久化备份；重要数据应在正确挂载的卷中。

## 5. 失败恢复

| 状态 | 含义 | 处理 |
|---|---|---|
| 待更新 | notify 模式发现新版本 | 按原部署方式更新，或审核后启用 auto |
| 失败 | 拉取、配置、验证等失败 | 查服务日志和 Docker 状态 |
| 已恢复旧镜像 | 新版本失败，旧镜像恢复验证通过 | 检查原因，决定是否再次尝试 |
| 待处理/needs_review | 操作中断、超时或恢复未验证成功 | 人工检查真实服务状态，禁止直接盲目重试 |

事务记录：`/opt/docker-update-bot/state-v2.json`。
配置快照：`/opt/docker-update-bot/transactions/目标ID-时间戳/`。

**恢复旧镜像只恢复程序镜像，不恢复数据库、文件或 schema 迁移。** 不应让不可逆迁移依赖这种镜像恢复。配置快照取自更新前磁盘上的 Compose 配置；如果原配置此前已被你改过而未部署，它不等于旧容器创建时的完整配置。

若已生成 `rollback.json`，你审核后可在服务器上手动恢复，例如：

```bash
docker compose --project-name myapp --project-directory /srv/myapp \
  -f /opt/docker-update-bot/transactions/实际事务目录/rollback.json \
  up -d --no-deps --no-build --pull never web
```

如果只有 `original.json`/`apply.json`，按 state-v2.json 里的 `old_tag` 审核准备恢复配置；不要直接拿 apply.json 当旧版本恢复文件。

确认服务状态、镜像和数据后，发送 `/ack web` 解除该目标的自动更新阻止。此命令不重建容器，不代表自动检测到问题已解决。解除后下一次巡检可能再次安装远端版本；问题未修复时不要解除。

## 6. 日常管理与升级

```bash
sudo systemctl status docker-update-bot --no-pager
sudo journalctl -u docker-update-bot -n 100 --no-pager
sudo systemctl restart docker-update-bot
sudo systemctl stop docker-update-bot
```

本版直接由 systemd 在异常退出后重启。Telegram 不可访问或进程已经退出期间，无法保证即时 Telegram 告警；恢复启动后会发启动消息并列出阻止的事务。要监控 Bot 自身彻底离线，需要独立的外部监控。

升级本版：停止正在执行的巡检前先等待其完成；上传并解压新的、审核过的发行包，运行新包内的 `sudo bash deploy.sh`，选择 1。包内依赖准备好后才切换服务。旧版本在 releases 下保留。

本版 `/update` 不再从上游 main 自动覆盖代码。当前交付未发布到 GitHub，原来的 curl 命令会安装原版。

请完整保留包内文件；安装器通过 SHA256SUMS 检查包的完整性。

## 7. 数据与清理

| 路径 | 说明 |
|---|---|
| `config.json` | Token、允许用户、Compose 目标 |
| `tasks.json` | 旧版任务，保留用于迁移 |
| `tasks-v2.json` | 当前监控目标 |
| `state-v2.json` | 最近结果、事务状态 |
| `transactions/` | 可能包含环境变量敏感信息的配置快照 |
| `releases/` | 程序各次安装版本和 venv |
| `current` | 当前版本符号链接 |
| `instance.lock` | 防止同时启动多个实例 |
| `ready.json` | 安装时确认进程已初始化，不是业务健康指标 |

以上路径均位于 `/opt/docker-update-bot`。不要公开上传真实配置、状态快照和环境文件。

本版保留 `check-docker-local/*` 本地镜像标签和事务，不自动清理。稳定运行并确认不再需要旧版本恢复后，可按 Docker 镜像 ID 和事务记录人工选择性清理。不要在升级/恢复过程中运行 prune 或其他部署工具。

卸载：在发行包目录运行 `sudo bash uninstall.sh`。默认只删除服务；第二次确认才删除 Bot 数据目录，永远不主动删除业务容器、Docker 镜像和卷。

## 8. 真实服务器上线前验收

先使用无重要数据的单副本测试服务，核对以下项目：

1. /scan、保存、/check、/status 正常工作，非白名单用户不能操作。
2. notify 模式仅提示更新，不重建容器。
3. auto 模式重建后实际镜像 ID 变化，挂载数据保留。
4. 新版本健康失败后恢复旧镜像，并暂停自动重试。
5. Telegram 通知失败不影响结果落盘，恢复连接后可通过 /status 查看。

本版不支持多副本滚动升级、Swarm、Kubernetes、Windows Docker，以及依赖交互 Shell 环境的隐式部署。
