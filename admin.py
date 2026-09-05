"""Local configuration initialization/migration; no network or Docker mutations."""
import argparse
import getpass
from pathlib import Path
import shutil
from core import atomic_json, load_config, read_json


def prepare(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / 'config.json'
    if not path.exists():
        token = getpass.getpass('Telegram Bot Token（输入不显示）: ').strip()
        chat = int(input('Chat ID: ').strip())
        users = [int(x.strip()) for x in input('允许操作的用户 ID（逗号分隔；私聊可留空）: ').split(',') if x.strip()]
        if not users and chat > 0:
            users = [chat]
        atomic_json(path, {'bot_token': token, 'chat_id': chat, 'allowed_user_ids': users,
                           'compose_targets': {}, 'check_interval': 36000, 'first_run_delay': 60})
    else:
        backup = root / 'config.before-v2.json'
        if not backup.exists():
            shutil.copy2(path, backup)
            backup.chmod(0o600)
    cfg = load_config(path)
    path.chmod(0o600)
    target = root / 'tasks-v2.json'
    if not target.exists():
        old = read_json(root / 'tasks.json', [])
        if not isinstance(old, list) or any(not isinstance(x, str) for x in old):
            raise ValueError('旧任务文件无效，已停止迁移；请修复 tasks.json')
        atomic_json(target, ['image:' + x for x in old])
    print(f'配置有效，允许 {len(cfg["allowed_user_ids"])} 个用户。旧版镜像任务已接入自动更新；可用 /scan 管理勾选。')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--data-dir', default='/opt/docker-update-bot')
    p.add_argument('--validate', action='store_true')
    args = p.parse_args()
    if args.validate:
        load_config(Path(args.data_dir) / 'config.json')
        print('配置验证通过')
    else:
        prepare(args.data_dir)
