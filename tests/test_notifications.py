import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from autoupdate_bot import BotUI, ProgressMessage


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_shows_image_name_instead_of_internal_container_key(self):
        text = BotUI.summary([{'task':'container:hubproxy', 'display':'ghcr.io/example/hubproxy:latest',
                               'status':'current', 'detail':'无更新，已跳过'}])
        self.assertIn('ghcr.io/example/hubproxy:latest', text)
        self.assertNotIn('container:hubproxy', text)
        self.assertIn('无更新，已跳过', text)

    async def test_status_reads_live_docker_and_does_not_repeat_cached_updated_result(self):
        engine=SimpleNamespace(
            tasks=['container:hubproxy'], lock=SimpleNamespace(locked=lambda:False),
            cfg={'chat_id':1,'allowed_user_ids':[1],'check_interval':36000},
            state={'last_check':'2026-09-06T04:17:20+00:00',
                   'last_results':[{'task':'container:hubproxy','status':'updated','detail':'旧缓存'}]},
            live_status=AsyncMock(return_value=[{'image':'ghcr.io/example/hubproxy:latest',
                                                 'name':'hubproxy','image_id':'sha256:abc',
                                                 'state':'运行中'}]))
        ui=BotUI(engine)
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
        update=SimpleNamespace(effective_chat=SimpleNamespace(id=1),effective_user=SimpleNamespace(id=1))
        context=SimpleNamespace(bot=bot)
        await ui.status(update,context)
        text=bot.send_message.await_args.kwargs['text']
        engine.live_status.assert_awaited_once()
        self.assertIn('实时 Docker 状态',text)
        self.assertIn('ghcr.io/example/hubproxy:latest',text)
        self.assertNotIn('旧缓存',text)
        self.assertNotIn('✅ 已更新',text)

    async def test_progress_message_is_edited_through_real_stages_and_shows_next_run(self):
        engine=SimpleNamespace(tasks=['image:demo:latest'], cfg={'chat_id':1}, state={})
        ui=BotUI(engine)
        message=SimpleNamespace(message_id=99)
        bot=SimpleNamespace(send_message=AsyncMock(return_value=message),
                            edit_message_text=AsyncMock())
        reporter=ProgressMessage(ui,bot,'定时')
        await reporter.start()
        await reporter({'stage':'checking_task','message':'检查','task':'demo:latest','index':1,'total':1})
        await reporter({'stage':'pulling','message':'正在拉取镜像 demo:latest',
                        'task':'image:demo:latest','index':1,'total':1})
        next_time=datetime(2026,9,6,8,0,0,tzinfo=ZoneInfo('Asia/Shanghai'))
        await reporter.finish([{'task':'image:demo:latest','display':'demo:latest',
                                'status':'updated','detail':'已重建并验证正常'}],next_time)
        edits=[call.kwargs['text'] for call in bot.edit_message_text.await_args_list]
        self.assertIn('正在查询远端仓库并检测新版本',edits[0])
        self.assertIn('进度：1/1',edits[0])
        report=bot.send_message.await_args_list[-1].kwargs['text']
        self.assertIn('运行日志报告',report)
        self.assertIn('下次巡检：2026-09-06 08:00:00',report)

    async def test_edit_failure_uses_replacement_without_raising(self):
        engine=SimpleNamespace(tasks=[],cfg={'chat_id':1},state={})
        ui=BotUI(engine)
        first=SimpleNamespace(message_id=1)
        second=SimpleNamespace(message_id=2)
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=[first,second,second,second]),
                            edit_message_text=AsyncMock(side_effect=RuntimeError('offline')))
        reporter=ProgressMessage(ui,bot,'手动')
        await reporter.start()
        await reporter({'stage':'checking_task','message':'检查','task':'nginx:latest','index':1,'total':1})
        await reporter({'stage':'inspecting','message':'读取配置','task':'nginx:latest'})
        self.assertEqual(reporter.message.message_id,2)

    async def test_realtime_messages_only_claim_update_after_ids_differ(self):
        engine=SimpleNamespace(tasks=['image:nginx:latest'],cfg={'chat_id':1},state={})
        ui=BotUI(engine)
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
                            edit_message_text=AsyncMock())
        reporter=ProgressMessage(ui,bot,'定时')
        await reporter.start()
        await reporter({'stage':'checking_task','message':'检查','task':'nginx:latest','index':1,'total':1})
        await reporter({'stage':'current','message':'无更新','task':'nginx:latest'})
        current=bot.edit_message_text.await_args.kwargs['text']
        self.assertIn('没有更新',current)
        self.assertNotIn('发现新版本',current)
        await reporter({'stage':'stopping','message':'停止','task':'nginx:latest',
                        'old_image':'sha256:old','new_image':'sha256:new'})
        changed=bot.edit_message_text.await_args.kwargs['text']
        self.assertIn('发现新版本',changed)
        self.assertIn('sha256:old',changed)
        self.assertIn('sha256:new',changed)


if __name__ == '__main__':
    unittest.main()
