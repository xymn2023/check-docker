import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Engine, Runner, atomic_json, read_json, load_config
from autoupdate_bot import BotUI


class FakeDocker:
    """In-memory Docker boundary. Runs actual engine orchestration, never Docker."""
    def __init__(self, target):
        self.target = target
        self.tags = {'demo:latest': 'sha256:old'}
        self.images = {'sha256:old', 'sha256:new'}
        self.current = {'Id': 'container1', 'Name': '/demo-web-1', 'Image': 'sha256:old',
                        'Config': {'Image': 'demo:latest', 'Labels': {'com.docker.compose.project': 'demo',
                                  'com.docker.compose.service': 'web'}},
                        'State': {'Running': True, 'Health': {'Status': 'healthy'}, 'StartedAt': '1'},
                        'RestartCount': 0}
        self.model = {'name': 'demo', 'services': {'web': {'image': 'demo:latest', 'environment': {'KEEP': 'yes'},
                      'volumes': [{'type': 'bind', 'source': '/srv/data', 'target': '/data'}]}}}
        self.calls = []
        self.new_healthy = True
        self.old_healthy = True
        self.pull_fail = False
        self.apply_timeout = False
        self.empty = False
        self.multi = False
        self.pull_gate = None
        self.config_drift = False
        self.config_count = 0

    async def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ['docker', 'compose']:
            if 'config' in args:
                self.config_count += 1
                model = copy.deepcopy(self.model)
                if self.config_drift and self.config_count > 1:
                    model['services']['web']['environment']['KEEP'] = 'changed'
                return json.dumps(model)
            if self.apply_timeout:
                raise TimeoutError('ambiguous daemon operation')
            path = args[args.index('-f') + 1]
            applied = json.loads(Path(path).read_text())
            ref = applied['services']['web']['image']
            ident = self.tags[ref]
            self.current['Id'] += 'x'
            self.current['Image'] = ident
            self.current['Config']['Image'] = ref
            if 'Health' in self.current['State']:
                self.current['State']['Health']['Status'] = 'healthy' if (self.new_healthy if ident == 'sha256:new' else self.old_healthy) else 'unhealthy'
            return ''
        if args[1] == 'ps':
            if self.empty:
                return ''
            return self.current['Id'] + (' extra' if self.multi else '')
        if args[1:3] == ['container', 'inspect']:
            return json.dumps([self.current])
        if args[1:3] == ['image', 'inspect']:
            ident = self.tags.get(args[3], args[3])
            return json.dumps([{'Id': ident, 'Os': 'linux', 'Architecture': 'amd64', 'RepoTags': [t for t, i in self.tags.items() if i == ident]}])
        if args[1] == 'pull':
            if self.pull_gate:
                await self.pull_gate.wait()
            if self.pull_fail:
                raise RuntimeError('pull failed')
            self.tags[args[-1]] = 'sha256:new'
            return ''
        if args[1:3] == ['image', 'tag']:
            self.tags[args[-1]] = args[-2]
            return ''
        if args[1:3] == ['image', 'rm']:
            ident = self.tags.pop(args[-1], args[-1])
            if ident not in self.tags.values():
                self.images.discard(ident)
            return ''
        if args[1:3] == ['image', 'ls']:
            return ' '.join(self.images)
        raise AssertionError(args)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = {'directory': self.tmp.name, 'files': [str(self.root/'compose.json')],
                       'project': 'demo', 'service': 'web', 'mode': 'auto'}
        self.cfg = {'compose_targets': {'web': self.target}, 'command_timeout': 10, 'pull_timeout': 10,
                    'health_timeout': .05, 'stability_seconds': 0, 'notify_unchanged': False,
                    'chat_id': 1, 'allowed_user_ids': [1]}
        self.fake = FakeDocker(self.target)
        self.engine = Engine(self.cfg, self.root, self.fake)
        self.engine.save_tasks(['compose:web'])
        self.notify = AsyncMock()

    async def test_recreates_and_preserves_config_with_exact_image(self):
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'updated')
        self.assertEqual(self.fake.current['Image'], 'sha256:new')
        calls = [a for a in self.fake.calls if 'up' in a]
        self.assertEqual(len(calls), 1)
        self.assertIn('--no-deps', calls[0])
        applied = read_json(Path(calls[0][calls[0].index('-f')+1]), {})
        self.assertEqual(applied['services']['web']['volumes'], self.fake.model['services']['web']['volumes'])
        self.assertNotIn('restart', [a[1] for a in self.fake.calls])

    async def test_already_pulled_still_updates_old_container(self):
        self.fake.tags['demo:latest'] = 'sha256:new'
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'updated')

    async def test_no_container_never_reports_success(self):
        self.fake.empty = True
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'error')
        self.assertFalse(any('up' in a for a in self.fake.calls))

    async def test_multi_replica_rejected(self):
        self.fake.multi = True
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'error')

    async def test_health_failure_restores_old_image_and_blocks_repeat(self):
        self.fake.new_healthy = False
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'rolled_back')
        self.assertEqual(self.fake.current['Image'], 'sha256:old')
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'blocked')
        self.engine.acknowledge('web')
        self.assertEqual(self.engine.state['transactions']['compose:web']['status'], 'acknowledged')

    async def test_rollback_failure_blocks(self):
        self.fake.new_healthy = self.fake.old_healthy = False
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'error')
        self.assertEqual(self.engine.state['transactions']['compose:web']['status'], 'needs_review')

    async def test_timeout_does_not_race_rollback(self):
        self.fake.apply_timeout = True
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'error')
        self.assertEqual(len([a for a in self.fake.calls if 'up' in a]), 1)
        self.assertEqual(self.engine.state['transactions']['compose:web']['status'], 'needs_review')

    async def test_notify_failure_does_not_hold_lock_or_prevent_update(self):
        self.notify.side_effect = RuntimeError('telegram offline')
        await self.engine.check(self.notify, manual=True)
        self.assertFalse(self.engine.lock.locked())
        self.assertEqual(self.fake.current['Image'], 'sha256:new')
        rows = await self.engine.check(self.notify, manual=True)
        self.assertEqual(rows[0]['status'], 'current')

    async def test_one_failure_does_not_skip_next_task(self):
        self.engine.save_tasks(['broken', 'compose:web'])
        rows = await self.engine.check(self.notify)
        self.assertEqual([r['status'] for r in rows if r['status'] != 'cleanup'], ['error', 'updated'])

    async def test_pull_failure_does_not_apply(self):
        self.fake.pull_fail = True
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'error')
        self.assertFalse(any('up' in a for a in self.fake.calls))

    async def test_old_notify_setting_now_automatically_updates(self):
        self.target['mode'] = 'notify'
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'updated')
        self.assertTrue(any('up' in a for a in self.fake.calls))

    async def test_ordinary_container_dispatches_to_automatic_updater(self):
        self.engine.save_tasks(['container:demo-web-1'])
        with patch('container_update.update', new=AsyncMock(return_value=('updated', 'ok'))) as updater:
            rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'updated')
        updater.assert_awaited_once()

    async def test_config_drift_cancels_before_apply(self):
        self.fake.config_drift = True
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'error')
        self.assertFalse(any('up' in a for a in self.fake.calls))

    async def test_missing_healthcheck_uses_running_verification(self):
        self.fake.current['State'].pop('Health')
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'updated')

    async def test_cancel_releases_lock(self):
        self.fake.pull_gate = asyncio.Event()
        job = asyncio.create_task(self.engine.check(self.notify))
        await asyncio.sleep(.01)
        self.assertIsNone(await self.engine.check(self.notify))
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertFalse(self.engine.lock.locked())

    async def test_restart_marks_incomplete_transaction(self):
        self.engine.state['transactions']['compose:web'] = {'status': 'applying'}
        self.engine.persist()
        restarted = Engine(self.cfg, self.root, self.fake)
        self.assertEqual(restarted.state['transactions']['compose:web']['status'], 'needs_review')

    async def test_write_failure_preserves_in_memory_tasks(self):
        with patch('core.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.engine.save_tasks([])
        self.assertEqual(self.engine.tasks, ['compose:web'])

    async def test_long_names_use_short_callback_ids(self):
        ui = BotUI(self.engine)
        ui.session = {'items': [('container:'+'a'*200, 'a'*200)], 'selected': set(), 'page': 0, 'token': '12345678'}
        for row in ui.keyboard().inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)

    async def test_auth_checks_user_as_well_as_chat(self):
        from types import SimpleNamespace as NS
        ui = BotUI(self.engine)
        self.assertFalse(ui.authorized(NS(effective_chat=NS(id=1), effective_user=NS(id=2))))
        self.assertTrue(ui.authorized(NS(effective_chat=NS(id=1), effective_user=NS(id=1))))

    async def test_runner_does_not_block_loop(self):
        ticked = False
        async def tick():
            nonlocal ticked
            await asyncio.sleep(.02)
            ticked = True
        job = asyncio.create_task(tick())
        out = await Runner()([sys.executable, '-c', 'import time;time.sleep(.1);print("ok")'])
        await job
        self.assertTrue(ticked)
        self.assertEqual(out, 'ok')

    async def test_runner_timeout(self):
        with self.assertRaises(TimeoutError):
            await Runner()([sys.executable, '-c', 'import time;time.sleep(5)'], timeout=.05)

    async def test_atomic_file_permissions(self):
        atomic_json(self.root/'private.json', {'secret': 'example'})
        self.assertEqual((self.root/'private.json').stat().st_mode & 0o777, 0o600)

    async def test_no_healthcheck_opt_in(self):
        self.fake.current['State'].pop('Health')
        self.target['allow_no_healthcheck'] = True
        await self.engine.verify(self.target, 'sha256:old', False)

    async def test_journal_write_failure_prevents_apply(self):
        with patch.object(self.engine, 'persist', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await self.engine.check(self.notify)
        self.assertFalse(any('up' in a for a in self.fake.calls))

    async def test_fixed_digest_is_not_pulled(self):
        self.engine.save_tasks(['container:demo-web-1'])
        self.fake.current['Config']['Image'] = 'demo@sha256:abcdef'
        rows = await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'], 'error')
        self.assertFalse(any(a[1] == 'pull' for a in self.fake.calls))

    async def test_migration_preserves_old_file_and_adds_pending_targets(self):
        from admin import prepare
        original = ['example:latest']
        atomic_json(self.root/'tasks.json', original)
        (self.root/'tasks-v2.json').unlink()
        atomic_json(self.root/'config.json', {'bot_token': 'example', 'chat_id': 1})
        prepare(self.root)
        self.assertEqual(read_json(self.root/'tasks.json', []), original)
        self.assertEqual(read_json(self.root/'tasks-v2.json', []), ['image:example:latest'])

    async def test_group_configuration_requires_users(self):
        atomic_json(self.root/'config.json', {'bot_token': 'example', 'chat_id': -100})
        with self.assertRaises(ValueError):
            load_config(self.root/'config.json')


if __name__ == '__main__':
    unittest.main()
