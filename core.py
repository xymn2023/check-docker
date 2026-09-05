"""Docker update engine. No Telegram dependency; all mutations are journaled."""
from __future__ import annotations
import asyncio
import copy
import json
import logging
import os
from pathlib import Path
import re
import signal
import tempfile
import time
from datetime import datetime, timezone

VERSION = '2.0.0'
LOG = logging.getLogger(__name__)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path, default):
    p = Path(path)
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else copy.deepcopy(default)


def load_config(path):
    cfg = read_json(path, {})
    cfg['bot_token'] = os.getenv('TELEGRAM_BOT_TOKEN') or cfg.get('bot_token', '')
    cfg['chat_id'] = int(os.getenv('ALLOWED_CHAT_ID') or cfg.get('chat_id', 0))
    users = cfg.get('allowed_user_ids')
    if users is None and cfg['chat_id'] > 0:
        users = [cfg['chat_id']]
    if not cfg['bot_token'] or not users or not cfg['chat_id']:
        raise ValueError('必须配置 bot_token、chat_id 和 allowed_user_ids（私聊可省略用户列表）')
    cfg['allowed_user_ids'] = [int(x) for x in users]
    for key, default, minimum in [('check_interval', 36000, 60), ('first_run_delay', 60, 1),
                                  ('pull_timeout', 1800, 1), ('command_timeout', 120, 1),
                                  ('health_timeout', 120, 1), ('stability_seconds', 15, 1)]:
        cfg[key] = int(cfg.get(key, default))
        if cfg[key] < minimum:
            raise ValueError(f'{key} 必须 >= {minimum}')
    if cfg['health_timeout'] <= cfg['stability_seconds']:
        raise ValueError('health_timeout 必须大于 stability_seconds')
    cfg.setdefault('notify_unchanged', False)
    if not isinstance(cfg['notify_unchanged'], bool):
        raise ValueError('notify_unchanged 必须是布尔值')
    cfg.setdefault('compose_targets', {})
    seen = set()
    for key, target in cfg['compose_targets'].items():
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}', key):
            raise ValueError('Compose 目标 ID 须为 1–32 位字母数字、下划线或连字符')
        for field in ('project', 'service'):
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', target[field]):
                raise ValueError(f'{key}: 非法 {field}')
        pair = (target['project'], target['service'])
        if pair in seen:
            raise ValueError('同一项目服务不能配置为多个目标')
        seen.add(pair)
        for p in [target['directory'], *target['files'], *target.get('env_files', [])]:
            if not Path(p).is_absolute() or not Path(p).exists():
                raise ValueError(f'{key}: 路径不存在或不是绝对路径: {p}')
        if not target['files'] or not Path(target['directory']).is_dir():
            raise ValueError(f'{key}: 必须配置工作目录和 Compose 文件')
        if not isinstance(target.get('allow_no_healthcheck', False), bool):
            raise ValueError(f'{key}: allow_no_healthcheck 必须是布尔值')
        if target.get('mode', 'notify') not in ('notify', 'auto'):
            raise ValueError(f'{key}: mode 仅支持 notify/auto')
    return cfg


class CommandError(RuntimeError):
    pass


class Runner:
    async def __call__(self, args, *, timeout=120, cwd=None):
        # No shell; stdout and stderr are separate. Bound retained output memory.
        p = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        async def drain(stream):
            result = bytearray()
            while chunk := await stream.read(65536):
                result.extend(chunk)
                if len(result) > 4 * 1024 * 1024:
                    del result[:len(result) - 4 * 1024 * 1024]
            return result.decode('utf-8', errors='replace')
        stdout = asyncio.create_task(drain(p.stdout))
        stderr = asyncio.create_task(drain(p.stderr))
        try:
            await asyncio.wait_for(p.wait(), timeout)
            out, err = await asyncio.gather(stdout, stderr)
        except BaseException:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await p.wait()
            await asyncio.gather(stdout, stderr, return_exceptions=True)
            raise
        if p.returncode:
            # Docker output may contain credentials: do not forward raw stderr to Telegram/logs.
            raise CommandError(f'{args[0]} {args[1] if len(args)>1 else ""} 失败，退出码 {p.returncode}；请在服务器上诊断')
        return out.strip()


class Engine:
    def __init__(self, cfg, data_dir, runner=None):
        self.cfg, self.root = cfg, Path(data_dir)
        self.run = runner or Runner()
        self.lock = asyncio.Lock()
        self.tasks_path = self.root / 'tasks-v2.json'
        self.state_path = self.root / 'state-v2.json'
        self.tasks = read_json(self.tasks_path, [])
        if not isinstance(self.tasks, list) or any(not isinstance(t, str) for t in self.tasks):
            raise ValueError('tasks-v2.json 必须是字符串数组')
        self.state = read_json(self.state_path, {'transactions': {}, 'last_results': [], 'last_check': None})
        self.state.setdefault('transactions', {})
        for tx in self.state['transactions'].values():
            if tx['status'] in ('applying', 'rolling_back'):
                tx['status'] = 'needs_review'
        self.persist()

    def persist(self):
        atomic_json(self.state_path, self.state)

    def save_tasks(self, tasks):
        selected = sorted(set(tasks))
        atomic_json(self.tasks_path, selected)
        self.tasks = selected

    async def docker(self, *args, timeout=None):
        return await self.run(['docker', *args], timeout=timeout or self.cfg['command_timeout'])

    async def inspect(self, ident, kind='container'):
        return json.loads(await self.docker(kind, 'inspect', ident))[0]

    async def containers(self, *filters):
        args = ['ps', '-aq']
        for f in filters:
            args += ['--filter', f]
        ids = (await self.docker(*args)).split()
        return [await self.inspect(cid) for cid in ids]

    async def service_containers(self, target):
        items = await self.containers('label=com.docker.compose.project=' + target['project'],
                                      'label=com.docker.compose.service=' + target['service'])
        return [c for c in items if (c['Config'].get('Labels') or {}).get('com.docker.compose.oneoff', '').lower() != 'true']

    async def catalog(self):
        choices = {'compose:' + k: f"Compose {t['project']}/{t['service']} [{t.get('mode','notify')}]"
                   for k, t in self.cfg['compose_targets'].items()}
        for c in await self.containers('status=running'):
            labels = c['Config'].get('Labels', {}) or {}
            if any(labels.get('com.docker.compose.project') == t['project'] and
                   labels.get('com.docker.compose.service') == t['service']
                   for t in self.cfg['compose_targets'].values()):
                continue
            name = c['Name'].lstrip('/')
            choices['container:' + name] = f"容器 {name} [仅通知]"
        for key in self.tasks:
            choices.setdefault(key, key + ' [历史任务，待检查]')
        return dict(sorted(choices.items()))

    def compose_args(self, t, files=None):
        args = ['docker', 'compose', '--project-name', t['project'], '--project-directory', t['directory']]
        for p in t.get('env_files', []):
            args += ['--env-file', p]
        for p in files or t['files']:
            args += ['-f', str(p)]
        return args

    async def compose_config(self, t):
        return json.loads(await self.run(self.compose_args(t) + ['config', '--format', 'json'],
                                        cwd=t['directory'], timeout=self.cfg['command_timeout']))

    async def pull(self, reference, current):
        if reference.startswith(('sha256:', '-')) or '@sha256:' in reference or re.fullmatch(r'[0-9a-f]{12,64}', reference):
            raise ValueError('固定 Digest/镜像 ID 不追踪更新；请使用可拉取标签')
        info = await self.inspect(current['Image'], 'image')
        platform = '/'.join(str(info[k]) for k in ('Os', 'Architecture', 'Variant') if info.get(k))
        await self.docker('pull', '--platform', platform, reference, timeout=self.cfg['pull_timeout'])
        return (await self.inspect(reference, 'image'))['Id']

    async def check_container(self, name):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', name):
            raise ValueError('非法容器名')
        c = await self.inspect(name)
        if not c['State']['Running']:
            return 'skipped', '容器已停止，保留监控任务'
        new = await self.pull(c['Config']['Image'], c)
        if new != c['Image']:
            return 'available', '新镜像已拉取；普通容器仅通知，请按原部署配置重建'
        return 'current', '运行中容器已使用最新镜像'

    @staticmethod
    def require_single(items):
        if len(items) != 1 or not items[0]['State']['Running']:
            raise ValueError('仅支持一个正在运行的服务容器；空服务、停机、多副本不自动处理')
        return items[0]

    async def apply(self, target, config, image, filename):
        snapshot = copy.deepcopy(config)
        snapshot['services'][target['service']]['image'] = image
        snapshot['services'][target['service']]['pull_policy'] = 'never'
        atomic_json(filename, snapshot)
        await self.run(self.compose_args(target, [filename]) + [
            'up', '-d', '--no-deps', '--no-build', '--pull', 'never', target['service']],
            cwd=target['directory'], timeout=self.cfg['command_timeout'])

    async def verify(self, target, expected, require_health):
        deadline = time.monotonic() + self.cfg['health_timeout']
        stable_since = None
        identity = None
        while time.monotonic() < deadline:
            items = await self.service_containers(target)
            if len(items) == 1:
                c = items[0]
                state = c['State']
                health = state.get('Health', {}).get('Status')
                ready = (c['Image'] == expected and state['Running'] and not state.get('Restarting')
                         and (health == 'healthy' if require_health else health in (None, 'healthy')))
                token = (c['Id'], c.get('RestartCount'), state.get('StartedAt'))
                if ready:
                    if token != identity or stable_since is None:
                        identity, stable_since = token, time.monotonic()
                    if time.monotonic() - stable_since >= self.cfg['stability_seconds']:
                        return
                else:
                    stable_since = None
            else:
                stable_since = None
            await asyncio.sleep(1)
        raise RuntimeError('容器镜像 ID、运行稳定性或健康检查未通过')

    async def check_compose(self, key):
        target = self.cfg['compose_targets'].get(key)
        if not target:
            raise ValueError('Compose 目标配置已移除；任务保留，请重新配置或取消勾选')
        task = 'compose:' + key
        tx = self.state['transactions'].get(task)
        if tx and tx['status'] in ('needs_review', 'rolled_back', 'applying', 'rolling_back'):
            return 'blocked', '上次事务需人工检查；处理后 /ack ' + key
        before = self.require_single(await self.service_containers(target))
        model = await self.compose_config(target)
        service = model['services'][target['service']]
        if int(service.get('deploy', {}).get('replicas', service.get('scale', 1))) != 1:
            raise ValueError('配置要求多副本，此版本不支持')
        reference = service.get('image')
        if not reference:
            raise ValueError('服务没有 image；本版本不自动构建镜像')
        auto = target.get('mode', 'notify') == 'auto'
        require_health = not target.get('allow_no_healthcheck', False)
        if auto and require_health and not before['State'].get('Health'):
            raise ValueError('自动更新要求 HEALTHCHECK；或显式 allow_no_healthcheck=true 使用运行稳定性检查')
        new = await self.pull(reference, before)
        if new == before['Image']:
            return 'current', '运行中服务已使用最新镜像'
        if not auto:
            return 'available', '新镜像已拉取；此 Compose 目标处于 notify 模式'
        # Check identity and resolved configuration again after a potentially long pull.
        now = self.require_single(await self.service_containers(target))
        if now['Id'] != before['Id'] or now['Image'] != before['Image'] or await self.compose_config(target) != model:
            raise RuntimeError('拉取期间容器或 Compose 配置发生变化，已取消应用')
        tag_base = 'check-docker-local/' + key.lower() + ':' + str(time.time_ns())
        old_tag, new_tag = tag_base + '-old', tag_base + '-new'
        await self.docker('image', 'tag', before['Image'], old_tag)
        await self.docker('image', 'tag', new, new_tag)
        folder = self.root / 'transactions' / (key + '-' + str(time.time_ns()))
        folder.mkdir(parents=True, mode=0o700)
        original = folder / 'original.json'
        atomic_json(original, model)
        tx = {'status': 'applying', 'started_at': stamp(), 'old_image': before['Image'],
              'new_image': new, 'old_tag': old_tag, 'new_tag': new_tag,
              'snapshot': str(original), 'project': target['project'], 'service': target['service']}
        self.state['transactions'][task] = tx
        self.persist()  # Must succeed before touching the service.
        try:
            await self.apply(target, model, new_tag, folder / 'apply.json')
            await self.verify(target, new, require_health)
        except (TimeoutError, asyncio.CancelledError):
            # Killing a Docker CLI cannot cancel daemon-side changes with certainty.
            tx['status'] = 'needs_review'
            self.persist()
            raise
        except Exception as update_error:
            tx['status'] = 'rolling_back'
            self.persist()
            try:
                await self.apply(target, model, old_tag, folder / 'rollback.json')
                await self.verify(target, before['Image'], require_health)
            except Exception:
                tx['status'] = 'needs_review'
                self.persist()
                raise RuntimeError('更新及旧镜像恢复未能确认成功，已阻止后续自动更新；请检查服务和事务文件') from update_error
            tx['status'] = 'rolled_back'
            tx['finished_at'] = stamp()
            self.persist()
            return 'rolled_back', '新版本验证失败，已恢复旧镜像并验证；检查后 /ack ' + key
        tx['status'] = 'completed'
        tx['finished_at'] = stamp()
        self.persist()
        return 'updated', '已重建容器，镜像 ID 和' + ('健康状态' if require_health else '运行稳定性（非业务健康）') + '验证通过'

    async def check(self, notify, manual=False):
        if self.lock.locked():
            return None
        async with self.lock:
            results = []
            for task in list(self.tasks):
                try:
                    if task.startswith('compose:'):
                        status, detail = await self.check_compose(task[8:])
                    elif task.startswith('container:'):
                        status, detail = await self.check_container(task[10:])
                    elif task.startswith('legacy:'):
                        status, detail = 'skipped', '旧版镜像任务尚未映射到容器；请 /scan 勾选新目标并取消旧任务'
                    else:
                        raise ValueError('未知任务类型')
                except Exception as exc:
                    status, detail = 'error', f'{type(exc).__name__}: {exc}'
                    LOG.warning('Task %s failed: %s', task, detail)
                results.append({'task': task, 'status': status, 'detail': detail})
            self.state['last_check'] = stamp()
            self.state['last_results'] = results
            self.persist()
        # Notifications happen outside the transaction and the lock.
        if manual or self.cfg['notify_unchanged'] or any(r['status'] != 'current' for r in results):
            try:
                await notify(results)
            except Exception:
                LOG.warning('Notification failed; saved result remains available through /status')
        return results

    def acknowledge(self, key):
        if self.lock.locked():
            raise ValueError('巡检执行中，不能解除事务阻止')
        tx = self.state['transactions'].get('compose:' + key)
        if not tx or tx['status'] not in ('needs_review', 'rolled_back'):
            raise ValueError('此目标没有待确认事务')
        previous = tx['status']
        tx['status'] = 'acknowledged'
        try:
            self.persist()
        except Exception:
            tx['status'] = previous
            raise
