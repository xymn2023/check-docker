"""Recreate an inspected container and preserve the original until verification."""
import asyncio
import copy
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote
from core import atomic_json, stamp

SOURCE_LABEL = 'io.check-docker.source-image'
TX_LABEL = 'io.check-docker.transaction'


def source_image(c):
    return (c['Config'].get('Labels') or {}).get(SOURCE_LABEL) or c['Config']['Image']


def endpoints(c):
    result = {}
    for network, endpoint in c.get('NetworkSettings', {}).get('Networks', {}).items():
        # Keep requested addresses (IPAMConfig), not dynamically assigned IPAddress.
        data = {k: copy.deepcopy(endpoint[k]) for k in ('IPAMConfig', 'Links', 'Aliases', 'DriverOpts', 'GwPriority')
                if endpoint.get(k) is not None}
        if data.get('Aliases'):
            data['Aliases'] = [a for a in data['Aliases'] if a not in (c['Id'], c['Id'][:12])]
        result[network] = data
    return result


def create_payload(c, new_image, reference, transaction):
    host = copy.deepcopy(c.get('HostConfig') or {})
    if host.get('AutoRemove'):
        raise ValueError('--rm 容器停机后会删除原容器，无法保证原容器回滚；请改为持久容器')
    if host.get('VolumesFrom'):
        raise ValueError('VolumesFrom 共享容器依赖需要先改成明确卷挂载，未停止原容器')
    if any(str(host.get(k, '')).startswith('container:') for k in ('NetworkMode', 'PidMode', 'IpcMode')):
        raise ValueError('共享其他容器命名空间，无法独立替换，未停止原容器')
    labels = c['Config'].get('Labels') or {}
    if any(k.startswith(('com.docker.swarm.', 'io.kubernetes.')) for k in labels):
        raise ValueError('编排器管理的容器应由编排器更新，未停止原容器')
    config = copy.deepcopy(c['Config'])
    config['Image'] = new_image
    config['Labels'] = dict(labels, **{SOURCE_LABEL: reference, TX_LABEL: transaction})
    # Old anonymous volumes must be explicitly rebound to their original volume names.
    destinations = {m.get('Target') for m in host.get('Mounts') or []}
    for bind in host.get('Binds') or []:
        pieces = bind.split(':')
        if len(pieces) >= 2:
            destinations.add(pieces[1])
    additions = []
    for mount in c.get('Mounts', []):
        dest = mount['Destination']
        if dest in destinations or dest in (host.get('Tmpfs') or {}):
            continue
        if mount['Type'] == 'volume':
            if not mount.get('Name'):
                raise ValueError('无法识别原卷名称，未停止原容器')
            additions.append({'Type': 'volume', 'Source': mount['Name'], 'Target': dest,
                              'ReadOnly': not mount.get('RW', True)})
        elif mount['Type'] == 'bind':
            additions.append({'Type': 'bind', 'Source': mount['Source'], 'Target': dest,
                              'ReadOnly': not mount.get('RW', True),
                              'BindOptions': {'Propagation': mount.get('Propagation') or 'rprivate'}})
        elif mount['Type'] != 'tmpfs':
            raise ValueError('不支持的挂载类型，未停止原容器：' + mount['Type'])
    host['Mounts'] = (host.get('Mounts') or []) + additions
    # Keep actual published host ports, including initially random assignments.
    ports = c.get('NetworkSettings', {}).get('Ports') or {}
    if ports:
        host['PortBindings'] = {k: copy.deepcopy(v) for k, v in ports.items() if v}
        host['PublishAllPorts'] = False
    config['HostConfig'] = host
    if host.get('NetworkMode') not in ('host', 'none'):
        config['NetworkingConfig'] = {'EndpointsConfig': endpoints(c)}
    return config


async def api(engine, endpoint, method, path, body=None, folder=None):
    args = [sys.executable, str(Path(__file__).with_name('docker_api.py')), endpoint, method, path]
    if body is not None:
        f = folder / ('api-' + str(time.time_ns()) + '.json')
        atomic_json(f, body)
        args.append(str(f))
    import json
    return json.loads(await engine.run(args, timeout=engine.cfg['command_timeout']))


async def verify_container(engine, ident, expected):
    deadline = time.monotonic() + engine.cfg['health_timeout']
    since, previous = None, None
    while time.monotonic() < deadline:
        c = await engine.inspect(ident)
        state = c['State']
        good = (c['Image'] == expected and state['Running'] and not state.get('Restarting')
                and state.get('Health', {}).get('Status') in (None, 'healthy'))
        identity = (c['Id'], state.get('StartedAt'), c.get('RestartCount'))
        if good:
            if previous != identity or since is None:
                since, previous = time.monotonic(), identity
            if time.monotonic() - since >= engine.cfg['stability_seconds']:
                return
        else:
            since = None
        await asyncio.sleep(1)
    raise RuntimeError('新容器镜像、健康检查或持续运行验证失败')


def restart_arg(policy):
    name = policy.get('Name') or 'no'
    if name == 'on-failure' and policy.get('MaximumRetryCount'):
        return name + ':' + str(policy['MaximumRetryCount'])
    return name


async def update(engine, before, new_image, reference):
    name = before['Name'].lstrip('/')
    task = 'container:' + name
    prior = engine.state['transactions'].get(task)
    if prior and (prior['status'] in ('needs_review', 'applying', 'rolling_back') or prior.get('cleanup_pending')):
        return 'blocked', name + ' 上次事务待处理；检查后 /ack container:' + name
    if prior and prior['status'] == 'rolled_back' and prior['new_image'] == new_image:
        return 'blocked', name + ' 已回滚此失败版本；其他新版本出现后自动重试，或 /ack container:' + name
    if before['Image'] == new_image:
        return 'current', name + ' 已使用最新镜像'
    transaction = str(time.time_ns())
    payload = create_payload(before, new_image, reference, transaction)
    # Other containers referencing this one's ID/name cannot follow a replacement.
    for other in await engine.containers():
        if other['Id'] == before['Id']:
            continue
        host = other.get('HostConfig') or {}
        for field in ('NetworkMode', 'PidMode', 'IpcMode'):
            ref = str(host.get(field, ''))
            if ref.startswith('container:') and ref[10:] in (name, before['Id'], before['Id'][:12]):
                raise ValueError('其他容器依赖此容器命名空间，未停止原容器')
        if any(v.split(':')[0] in (name, before['Id'], before['Id'][:12]) for v in host.get('VolumesFrom') or []):
            raise ValueError('其他容器通过 VolumesFrom 依赖此容器，未停止原容器')
    endpoint = (None if os.getenv('DOCKER_CONTEXT') else os.getenv('DOCKER_HOST')) or await engine.docker('context', 'inspect', '--format', '{{.Endpoints.docker.Host}}')
    if not endpoint.startswith('unix://'):
        raise ValueError('自动重建需要本机 Unix Docker socket，未停止原容器')
    await api(engine, endpoint, 'GET', '/info')  # Check API access before stopping anything.
    now = await engine.inspect(name)
    if now['Id'] != before['Id'] or now['Image'] != before['Image'] or not now['State']['Running']:
        raise ValueError('拉取期间容器发生变化，未停止原容器')
    folder = engine.root / 'transactions' / ('container-' + name + '-' + transaction)
    folder.mkdir(parents=True, mode=0o700)
    atomic_json(folder / 'original-container.json', before)
    atomic_json(folder / 'new-container.json', payload)
    tx = {'status': 'applying', 'started_at': stamp(), 'old_image': before['Image'],
          'new_image': new_image, 'old_container': before['Id'], 'new_container': None,
          'original_name': name, 'backup_name': name + '-cd-backup-' + transaction,
          'snapshot': str(folder / 'original-container.json'), 'cleanup_pending': False}
    engine.state['transactions'][task] = tx
    engine.persist()
    try:
        await engine.docker('update', '--restart', 'no', before['Id'])
        await engine.docker('stop', before['Id'])
        await engine.docker('rename', before['Id'], tx['backup_name'])
        for network in endpoints(before):
            if payload['HostConfig'].get('NetworkMode') not in ('host', 'none'):
                await engine.docker('network', 'disconnect', network, before['Id'])
        created = await api(engine, endpoint, 'POST', '/containers/create?name=' + quote(name, safe=''), payload, folder)
        tx['new_container'] = created['Id']
        engine.persist()
        await engine.docker('start', tx['new_container'])
        await verify_container(engine, tx['new_container'], new_image)
    except (TimeoutError, asyncio.CancelledError):
        tx['status'] = 'needs_review'
        engine.persist()
        raise
    except Exception as problem:
        tx['status'] = 'rolling_back'
        engine.persist()
        try:
            if tx['new_container']:
                await engine.docker('rm', '-f', tx['new_container'])  # Never -v.
            old = await engine.inspect(before['Id'])
            if old['Name'] != before['Name']:
                await engine.docker('rename', before['Id'], name)
            connected = old.get('NetworkSettings', {}).get('Networks', {})
            for network, ep in endpoints(before).items():
                if network not in connected and payload['HostConfig'].get('NetworkMode') not in ('host', 'none'):
                    await api(engine, endpoint, 'POST', '/networks/' + quote(network, safe='') + '/connect',
                              {'Container': before['Id'], 'EndpointConfig': ep}, folder)
            await engine.docker('update', '--restart', restart_arg(before.get('HostConfig', {}).get('RestartPolicy', {})), before['Id'])
            await engine.docker('start', before['Id'])
            await verify_container(engine, before['Id'], before['Image'])
        except BaseException:
            tx['status'] = 'needs_review'
            engine.persist()
            raise RuntimeError('恢复原容器未验证成功；已保留旧镜像和事务，请检查 ' + name) from problem
        tx['status'] = 'rolled_back'
        tx['finished_at'] = stamp()
        engine.persist()
        return 'rolled_back', name + ' 更新失败，已恢复原容器及旧镜像；检查后 /ack container:' + name
    tx['status'] = 'completed'
    tx['finished_at'] = stamp()
    tx['cleanup_pending'] = True
    engine.persist()
    engine.state.setdefault('cleanup_images', [])
    if before['Image'] not in engine.state['cleanup_images']:
        engine.state['cleanup_images'].append(before['Image'])
    engine.persist()
    return 'updated', name + ' 已重建并验证正常；旧容器及旧镜像进入清理队列'
