"""Telegram UI and lifecycle for check-docker v2."""
from __future__ import annotations
import asyncio
import fcntl
import logging
import os
from pathlib import Path
import secrets
import signal
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from core import Engine, VERSION, load_config, atomic_json, stamp

LOG = logging.getLogger(__name__)
PAGE_SIZE = 8
STATUS = {'current': '⏭️ 无更新，已跳过', 'available': '📥 待更新', 'updated': '✅ 已更新',
          'error': '❌ 失败', 'blocked': '⛔ 待处理', 'rolled_back': '↩️ 已恢复旧镜像', 'skipped': '⏭️ 跳过', 'cleanup': '🧹 清理结果'}
CN_TZ = ZoneInfo('Asia/Shanghai')


def cn_time(value=None):
    value = value or datetime.now(CN_TZ)
    return value.astimezone(CN_TZ).strftime('%Y-%m-%d %H:%M:%S')


def interval_text(seconds):
    if seconds % 3600 == 0:
        return f'{seconds // 3600} 小时'
    if seconds % 60 == 0:
        return f'{seconds // 60} 分钟'
    return f'{seconds} 秒'


class ProgressMessage:
    """Keep one live message per image, then send a complete run report."""
    def __init__(self, ui, bot, run_type):
        self.ui, self.bot, self.run_type = ui, bot, run_type
        self.message = None
        self.started = datetime.now(CN_TZ)
        self.last_text = None
        self.index = self.total = 0
        self.task = None

    async def start(self):
        if not self.ui.engine.tasks:
            self.message = await self.ui.send_message(self.bot, f'ℹ️【{self.run_type}巡检】没有已勾选的镜像任务')

    async def replace(self, text, new=False):
        if text == self.last_text:
            return
        self.last_text = text
        if self.message and not new:
            try:
                await self.bot.edit_message_text(chat_id=self.ui.cfg['chat_id'], message_id=self.message.message_id, text=text)
                return
            except Exception:
                LOG.warning('Could not edit progress message; sending a replacement')
        self.message = await self.ui.send_message(self.bot, text)

    def phase_text(self, title, detail):
        return (f'{title}【{self.run_type}巡检】\n📦 镜像：{self.task}\n'
                f'📊 进度：{self.index}/{self.total}\n{detail}\n'
                f'🕒 更新时间：{cn_time()}（北京时间）')

    async def __call__(self, event):
        stage = event['stage']
        if stage == 'checking_task':
            self.task = event.get('task') or '未知镜像'
            self.index, self.total = event.get('index', 0), event.get('total', 0)
            await self.replace(self.phase_text('🔍', f'正在检测 {self.task} 是否有更新…'), new=True)
            return
        if stage.startswith('cleanup_'):
            await self.ui.send(self.bot, f'🧹【{self.run_type}巡检·镜像清理】\n{event["message"]}\n🕒 {cn_time()}（北京时间）')
            return
        task = event.get('task') or self.task
        if task and task != '镜像清理':
            self.task = task
        details = {
            'inspecting': '📋 正在读取实际运行配置…',
            'matched': '🔗 ' + event['message'],
            'pulling': '🌐 正在查询远端仓库并检测新版本…',
            'pulled': '📥 远端镜像获取完成，正在对比实际镜像 ID…',
            'current': '🟢 镜像 ID 未变化，没有更新，本次已跳过。',
            'stopping': (f'🆕 发现新版本\n旧镜像：{event.get("old_image", "未知")[:19]}\n'
                         f'新镜像：{event.get("new_image", "未知")[:19]}\n⏸ 正在保留旧容器并准备重建…'),
            'recreating': '🔄 新版本已拉取，正在重建并启动容器…',
            'verifying': '🩺 容器已启动，正在验证镜像 ID、运行状态和健康状态…',
            'rolling_back': f'⚠️ 新版本启动或验证失败\n↩️ 正在回滚到 {event.get("old_image", "原镜像")[:19]}…',
            'verifying_rollback': '🛡 旧容器已启动，正在验证回滚结果…',
            'cleanup': '🧹 更新任务已完成，正在安全清理旧容器和旧镜像…',
        }
        if stage == 'recreating' and event.get('old_image') and event.get('new_image'):
            details['recreating'] = (f'🆕 发现新版本\n旧镜像：{event["old_image"][:19]}\n'
                                     f'新镜像：{event["new_image"][:19]}\n'
                                     '🔄 新版本已拉取，正在重建并启动容器…')
        if stage == 'task_done':
            status = event.get('status')
            title = {'updated':'✅','current':'🟢','rolled_back':'↩️','error':'❌','blocked':'⛔','skipped':'⏭️'}.get(status, 'ℹ️')
            await self.replace(self.phase_text(title, f'{STATUS.get(status, status)}\n{event.get("detail", event["message"])}'))
        elif stage in details:
            await self.replace(self.phase_text('⏳', details[stage]))

    async def finish(self, results, next_time=None):
        counts = {}
        for row in results or []:
            counts[row['status']] = counts.get(row['status'], 0) + 1
        stats = '，'.join(f'{STATUS.get(k, k)} {v}' for k, v in counts.items()) or '无任务'
        duration = max(0, int((datetime.now(CN_TZ) - self.started).total_seconds()))
        text = (f'📋【{self.run_type}巡检】运行日志报告\n'
                f'开始时间：{cn_time(self.started)}（北京时间）\n'
                f'完成时间：{cn_time()}（北京时间）\n'
                f'总耗时：{duration} 秒\n结果统计：{stats}\n\n' + self.ui.summary(results))
        if next_time:
            text += f'\n下次巡检：{cn_time(next_time)}（北京时间）'
        await self.ui.send(self.bot, text)


class BotUI:
    def __init__(self, engine):
        self.engine = engine
        self.cfg = engine.cfg
        self.session = None
        self.manual_task = None
        self.current_progress = None
        self.next_patrol_at = None

    def authorized(self, update):
        return bool(update.effective_chat and update.effective_user
                    and update.effective_chat.id == self.cfg['chat_id']
                    and update.effective_user.id in self.cfg['allowed_user_ids'])

    async def send(self, bot, text):
        # Plain text avoids Markdown injection, 1500 codepoints also fits UTF-16 limits.
        for start in range(0, len(text), 1500):
            for attempt in range(3):
                try:
                    await bot.send_message(chat_id=self.cfg['chat_id'], text=text[start:start+1500])
                    break
                except Exception:
                    if attempt == 2:
                        LOG.warning('Telegram notification unavailable (content suppressed)')
                        return False
                    await asyncio.sleep(2 ** attempt)
        return True

    async def send_message(self, bot, text):
        """Send one control/progress message and return it for later edits."""
        for attempt in range(3):
            try:
                return await bot.send_message(chat_id=self.cfg['chat_id'], text=text)
            except Exception:
                if attempt == 2:
                    LOG.warning('Telegram progress message unavailable')
                    return None
                await asyncio.sleep(2 ** attempt)

    @staticmethod
    def summary(results):
        if not results:
            return '任务池为空。请 /scan 勾选监控目标。'
        return '巡检结果\n\n' + '\n\n'.join(
            f"{STATUS.get(r['status'], r['status'])} · {r.get('display') or r['task']}\n{r['detail']}" for r in results)

    async def start(self, update, context):
        if not self.authorized(update):
            return
        await self.send(context.bot, f'check-docker {VERSION}\n'
                        '/scan 扫描并管理任务\n/check 立即巡检\n/status 状态及上次结果\n'
                        '/ack compose:目标ID 或 container:容器名 确认已人工处理失败事务，允许再次更新\n'
                        '/update 查看程序升级方式\n'
                        '勾选镜像后自动巡检、拉取、重建；成功清理旧镜像，失败恢复旧容器。')

    async def status(self, update, context):
        if not self.authorized(update):
            return
        last = self.engine.state.get('last_check')
        if last:
            last = datetime.fromisoformat(last).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S 北京时间')
        progress = self.current_progress.last_text if self.current_progress else None
        next_line = f"\n下次巡检：{cn_time(self.next_patrol_at)}（北京时间）" if self.next_patrol_at else ''
        try:
            live = await self.engine.live_status()
            live_text = ('\n\n'.join(f"镜像：{row['image']}\n容器：{row['name']}\n"
                                     f"实际镜像 ID：{row['image_id']}\n状态：{row['state']}"
                                     for row in live) or '当前没有 Docker 容器')
        except Exception as exc:
            live_text = f'实时读取 Docker 失败：{type(exc).__name__}: {exc}'
        await self.send(context.bot, f"check-docker {VERSION}\n状态：{'巡检中' if self.engine.lock.locked() else '待命'}\n"
                        f"监控目标：{len(self.engine.tasks)}\n间隔：{self.cfg['check_interval']} 秒\n"
                        f"上次完成时间：{last or '尚未执行'}{next_line}\n\n"
                        + ((progress + '\n\n') if progress else '')
                        + f'实时 Docker 状态（查询时间：{cn_time()} 北京时间）\n\n{live_text}')

    async def check(self, update, context):
        if not self.authorized(update):
            return
        if self.engine.lock.locked() or (self.manual_task and not self.manual_task.done()):
            await self.send(context.bot, '已有巡检执行中，请稍后查看 /status。')
            return
        async def work():
            reporter = ProgressMessage(self, context.bot, '手动')
            self.current_progress = reporter
            try:
                await reporter.start()
                rows = await self.engine.check(lambda result: asyncio.sleep(0),
                                               manual=True, progress=reporter)
                await reporter.finish(rows)
            except Exception:
                LOG.exception('Patrol failed outside task execution')
                await self.send(context.bot, '巡检异常，未能保存结果。请检查磁盘和服务日志。')
            finally:
                self.current_progress = None
        # Independent job keeps /status and callbacks responsive during pulls.
        self.manual_task = asyncio.create_task(work())

    def keyboard(self):
        s = self.session
        start = s['page'] * PAGE_SIZE
        rows = []
        for index in range(start, min(start + PAGE_SIZE, len(s['items']))):
            key, label = s['items'][index]
            rows.append([InlineKeyboardButton(('☑️ ' if key in s['selected'] else '⬜ ') + label[:90],
                                               callback_data=f"{s['token']}:t:{index}")])
        rows.append([InlineKeyboardButton('上一页', callback_data=f"{s['token']}:p:-1"),
                     InlineKeyboardButton('下一页', callback_data=f"{s['token']}:p:1")])
        rows.append([InlineKeyboardButton('全选', callback_data=f"{s['token']}:a:0"),
                     InlineKeyboardButton('全不选', callback_data=f"{s['token']}:n:0"),
                     InlineKeyboardButton('保存', callback_data=f"{s['token']}:s:0")])
        return InlineKeyboardMarkup(rows)

    async def scan(self, update, context):
        if not self.authorized(update):
            return
        catalog = await self.engine.catalog()
        self.session = {'token': secrets.token_hex(4), 'items': list(catalog.items()),
                        'selected': set(self.engine.tasks), 'page': 0}
        await update.message.reply_text('勾选需要自动更新的镜像（保存后生效；成功清理旧镜像，失败回滚）：',
                                        reply_markup=self.keyboard())

    async def callback(self, update, context):
        if not self.authorized(update):
            await update.callback_query.answer('无权操作', show_alert=True)
            return
        query = update.callback_query
        parts = (query.data or '').split(':')
        if len(parts) != 3 or not self.session or parts[0] != self.session['token']:
            await query.answer('面板已过期，请重新 /scan', show_alert=True)
            return
        await query.answer()
        _, action, value = parts
        s = self.session
        if action == 't':
            index = int(value)
            if not 0 <= index < len(s['items']):
                return
            key = s['items'][index][0]
            s['selected'].symmetric_difference_update({key})
        elif action == 'a':
            s['selected'] = {key for key, _ in s['items']}
        elif action == 'n':
            s['selected'] = set()
        elif action == 'p':
            s['page'] = max(0, min(s['page'] + int(value), max(0, (len(s['items'])-1)//PAGE_SIZE)))
        elif action == 's':
            self.engine.save_tasks(s['selected'])
            self.session = None
            await query.edit_message_text(f'已保存 {len(self.engine.tasks)} 个任务，下次巡检生效。')
            return
        try:
            await query.edit_message_reply_markup(reply_markup=self.keyboard())
        except Exception as exc:
            if 'Message is not modified' not in str(exc):
                raise

    async def ack(self, update, context):
        if not self.authorized(update):
            return
        if len(context.args) != 1:
            await self.send(context.bot, '用法：/ack compose:目标ID 或 container:容器名\n仅在服务器上检查并处理失败事务后使用。此命令不恢复服务，只解除自动更新阻止。')
            return
        self.engine.acknowledge(context.args[0])
        await self.send(context.bot, '已确认处理。该目标将在下次巡检重新检查；现在没有执行容器操作。')

    async def update_code(self, update, context):
        if self.authorized(update):
            await self.send(context.bot, f'当前版本 {VERSION}。请在服务器以 root 重新执行一键命令，选择菜单 1 升级：\n'
                            'bash <(curl -fsSL https://raw.githubusercontent.com/xymn2023/check-docker/main/deploy.sh)\n'
                            '安装器会固定 GitHub Commit 下载，准备依赖后切换服务并保留旧版本。升级前请等待当前巡检完成。')

    async def on_error(self, update, context):
        LOG.error('Telegram handler failure: %s', type(context.error).__name__)
        if update and self.authorized(update):
            await self.send(context.bot, '操作未完成：请检查配置、磁盘和服务日志后重试；未确认写入的任务不会显示保存成功。')

    async def patrol(self, bot):
        self.next_patrol_at = datetime.now(CN_TZ) + timedelta(seconds=self.cfg['first_run_delay'])
        await self.send(bot, f'⏰ 定时巡检已安排\n'
                             f'首次巡检：{cn_time(self.next_patrol_at)}（北京时间）\n'
                             f'后续间隔：每 {interval_text(self.cfg["check_interval"])}\n'
                             f'监控任务：{len(self.engine.tasks)} 个')
        await asyncio.sleep(self.cfg['first_run_delay'])
        while True:
            reporter = ProgressMessage(self, bot, '定时')
            self.current_progress = reporter
            try:
                await reporter.start()
                rows = await self.engine.check(lambda result: asyncio.sleep(0),
                                               manual=True, progress=reporter)
                self.next_patrol_at = datetime.now(CN_TZ) + timedelta(seconds=self.cfg['check_interval'])
                await reporter.finish(rows, self.next_patrol_at)
            except Exception:
                LOG.exception('Scheduled patrol failed')
                self.next_patrol_at = datetime.now(CN_TZ) + timedelta(seconds=self.cfg['check_interval'])
                await self.send(bot, '❌ 定时巡检异常\n'
                                     f'发生时间：{cn_time()}（北京时间）\n'
                                     f'下次重试：{cn_time(self.next_patrol_at)}（北京时间）\n'
                                     '请检查服务日志和磁盘。')
            finally:
                self.current_progress = None
            await asyncio.sleep(self.cfg['check_interval'])


async def serve(engine):
    ui = BotUI(engine)
    app = Application.builder().token(engine.cfg['bot_token']).build()
    for command, handler in [('start', ui.start), ('help', ui.start), ('status', ui.status),
                             ('scan', ui.scan), ('check', ui.check), ('ack', ui.ack), ('update', ui.update_code)]:
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(ui.callback))
    app.add_error_handler(ui.on_error)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        atomic_json(engine.root / 'ready.json', {'version': VERSION, 'at': stamp()})
        worker = asyncio.create_task(ui.patrol(app.bot))
        try:
            try:
                await app.bot.set_my_commands([BotCommand('scan', '管理监控任务'), BotCommand('check', '立即巡检'),
                                              BotCommand('status', '查看状态'), BotCommand('help', '命令说明')])
            except Exception:
                LOG.warning('Could not set command menu')
            blocked = [k for k, v in engine.state['transactions'].items() if v['status'] in ('needs_review','rolled_back')]
            await ui.send(app.bot, f'check-docker {VERSION} 已启动。首次巡检 {engine.cfg["first_run_delay"]} 秒后。'
                          + ('\n需要人工检查：' + ', '.join(blocked) if blocked else ''))
            await stop.wait()
        finally:
            (engine.root / 'ready.json').unlink(missing_ok=True)
            await app.updater.stop()
            jobs = [worker] + ([ui.manual_task] if ui.manual_task else [])
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await app.stop()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    root = Path(os.getenv('CHECK_DOCKER_DATA_DIR', '/opt/docker-update-bot'))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    with (root / 'instance.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('已有实例运行')
        cfg = load_config(root / 'config.json')
        asyncio.run(serve(Engine(cfg, root)))


if __name__ == '__main__':
    main()
