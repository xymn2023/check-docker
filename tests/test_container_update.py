import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from core import Engine
from container_update import create_payload, source_image, SOURCE_LABEL


def container():
    return {'Id': 'c-old', 'Name': '/web', 'Image': 'sha256:old',
            'Config': {'Image': 'demo:latest', 'Env': ['KEY=value'], 'Cmd': ['serve'],
                       'Labels': {}, 'Volumes': {'/data': {}}},
            'HostConfig': {'RestartPolicy': {'Name': 'always'}, 'NetworkMode': 'private',
                           'Binds': ['/srv/site:/site:ro'], 'Mounts': [], 'Memory': 123456,
                           'PortBindings': {'80/tcp': [{'HostIp': '0.0.0.0', 'HostPort': ''}]}},
            'Mounts': [{'Type': 'volume', 'Name': 'original-anonymous-volume', 'Destination': '/data', 'RW': True},
                       {'Type': 'bind', 'Source': '/srv/site', 'Destination': '/site', 'RW': False}],
            'State': {'Running': True, 'Health': {'Status': 'healthy'}, 'StartedAt': 'start-old'},
            'RestartCount': 0,
            'NetworkSettings': {'Ports': {'80/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '32789'}]},
                                'Networks': {'private': {'Aliases': ['web'], 'IPAMConfig': {'IPv4Address': '172.25.0.4'}}}}}


class ContainerDocker:
    def __init__(self):
        self.items = {'c-old': container()}
        self.tags = {'demo:latest': 'sha256:old'}
        self.images = {'sha256:old','sha256:new'}
        self.calls = []
        self.new_health = 'healthy'
        self.create_failure = False
        self.create_timeout = False
        self.remove_failure = False
        self.api_bodies = []

    def find(self, ident):
        return next(c for c in self.items.values() if c['Id']==ident or c['Name'].lstrip('/')==ident)

    async def __call__(self, a, **kwargs):
        import json
        self.calls.append(a)
        if a[1] == 'ps':
            items = self.items.values()
            if 'status=running' in a:
                items = [c for c in items if c['State']['Running']]
            return ' '.join(c['Id'] for c in items)
        if a[1:3] == ['container','inspect']:
            return json.dumps([self.find(a[-1])])
        if a[1:3] == ['image','inspect']:
            ident = self.tags.get(a[-1],a[-1])
            return json.dumps([{'Id': ident, 'Os':'linux','Architecture':'amd64',
                                'RepoTags':[t for t,i in self.tags.items() if i==ident]}])
        if a[1]=='pull':
            self.tags[a[-1]]='sha256:new'
            return ''
        if a[1]=='context': return 'unix:///fake/docker.sock'
        if a[1]=='update':
            self.find(a[-1])['HostConfig']['RestartPolicy']={'Name':a[-2]}
            return ''
        if a[1]=='stop':
            self.find(a[-1])['State']['Running']=False
            return ''
        if a[1]=='rename':
            self.find(a[-2])['Name']='/'+a[-1]
            return ''
        if a[1:3]==['network','disconnect']:
            self.find(a[-1])['NetworkSettings']['Networks'].pop(a[-2],None)
            return ''
        if a[1]=='start':
            self.find(a[-1])['State']['Running']=True
            return ''
        if a[1]=='rm':
            if self.remove_failure and a[-1]=='c-old': raise RuntimeError('busy')
            self.items.pop(a[-1])
            return ''
        if a[1:3]==['image','rm']:
            self.images.discard(a[-1])
            return ''
        if a[1:3]==['image','ls']:
            return ' '.join(self.images)
        raise AssertionError(a)

    async def api(self, engine, endpoint, method, path, body=None, folder=None):
        self.api_bodies.append(copy.deepcopy(body))
        if method=='GET': return {}
        if path.startswith('/containers/create'):
            if self.create_timeout: raise TimeoutError('unknown result')
            if self.create_failure: raise RuntimeError('create rejected')
            c=container()
            c['Id']='c-new' if 'c-new' not in self.items else 'c-new2'
            from urllib.parse import unquote
            c['Name']='/' + unquote(path.split('name=',1)[1])
            c['Image']=body['Image']
            c['Config']={k:v for k,v in body.items() if k not in ('HostConfig','NetworkingConfig')}
            c['HostConfig']=copy.deepcopy(body['HostConfig'])
            c['State']['Running']=False
            c['State']['Health']['Status']=self.new_health
            c['State']['StartedAt']='start-new'
            self.items[c['Id']]=c
            return {'Id':c['Id']}
        if path.endswith('/connect'):
            self.find(body['Container'])['NetworkSettings']['Networks']['private']=copy.deepcopy(body['EndpointConfig'])
            return {}
        raise AssertionError(path)


class ContainerUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg={'compose_targets':{},'command_timeout':10,'pull_timeout':10,'health_timeout':.02,
                  'stability_seconds':0,'notify_unchanged':False}
        self.fake=ContainerDocker()
        self.engine=Engine(self.cfg,Path(self.tmp.name),self.fake)
        self.engine.save_tasks(['image:demo:latest'])
        self.patch=patch('container_update.api',new=self.fake.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.notify=AsyncMock()

    async def test_selected_image_updates_then_deletes_old_container_and_image(self):
        rows=await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'],'updated')
        self.assertNotIn('c-old',self.fake.items)
        self.assertNotIn('sha256:old',self.fake.images)
        self.assertEqual(self.fake.items['c-new']['Image'],'sha256:new')
        self.assertEqual(source_image(self.fake.items['c-new']),'demo:latest')
        self.assertTrue(any(a[:3]==['docker','image','rm'] for a in self.fake.calls))

    async def test_selected_image_updates_all_associated_containers(self):
        second=container();second['Id']='c-old2';second['Name']='/web2'
        self.fake.items[second['Id']]=second
        rows=await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'],'updated')
        self.assertNotIn('c-old',self.fake.items)
        self.assertNotIn('c-old2',self.fake.items)
        self.assertEqual({c['Name'] for c in self.fake.items.values()}, {'/web','/web2'})
        self.assertTrue(all(c['Image']=='sha256:new' for c in self.fake.items.values()))
        self.assertNotIn('sha256:old',self.fake.images)

    async def test_unhealthy_new_container_restores_original_without_deleting_old_image(self):
        self.fake.new_health='unhealthy'
        rows=await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'],'rolled_back')
        self.assertNotIn('c-new',self.fake.items)
        old=self.fake.items['c-old']
        self.assertTrue(old['State']['Running'])
        self.assertEqual(old['Name'],'/web')
        self.assertEqual(old['HostConfig']['RestartPolicy']['Name'],'always')
        self.assertIn('private',old['NetworkSettings']['Networks'])
        self.assertIn('sha256:old',self.fake.images)
        self.assertFalse(any(a[1:3]==['image','rm'] for a in self.fake.calls))
        self.engine.acknowledge('container:web')

    async def test_create_failure_restores_old_container(self):
        self.fake.create_failure=True
        rows=await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'],'rolled_back')
        self.assertEqual(self.fake.items['c-old']['Name'],'/web')
        self.assertTrue(self.fake.items['c-old']['State']['Running'])

    async def test_api_timeout_keeps_old_container_and_image_for_review(self):
        self.fake.create_timeout=True
        rows=await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'],'error')
        self.assertEqual(self.engine.state['transactions']['container:web']['status'],'needs_review')
        self.assertIn('c-old',self.fake.items)
        self.assertIn('sha256:old',self.fake.images)

    async def test_shared_old_image_is_kept_for_stopped_container(self):
        c=container();c['Id']='c-other';c['Name']='/other';c['State']['Running']=False
        self.fake.items[c['Id']]=c
        await self.engine.check(self.notify)
        self.assertIn('sha256:old',self.fake.images)
        self.assertIn('sha256:old',self.engine.state['cleanup_images'])

    async def test_failed_backup_cleanup_retries_without_rolling_back_new_service(self):
        self.fake.remove_failure=True
        rows=await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'],'updated')
        self.assertIn('c-old',self.fake.items)
        self.fake.remove_failure=False
        await self.engine.check(self.notify)
        self.assertNotIn('c-old',self.fake.items)
        self.assertNotIn('sha256:old',self.fake.images)

    async def test_scan_groups_images_and_says_automatic(self):
        choices=await self.engine.catalog()
        self.assertIn('image:demo:latest',choices)
        self.assertIn('自动更新',choices['image:demo:latest'])
        self.assertFalse(any('仅通知' in s for s in choices.values()))

    async def test_payload_preserves_mounts_ports_resources_and_static_network(self):
        p=create_payload(container(),'sha256:new','demo:latest','tx')
        self.assertEqual(p['Env'],['KEY=value'])
        self.assertEqual(p['HostConfig']['Memory'],123456)
        self.assertEqual(p['HostConfig']['Binds'],['/srv/site:/site:ro'])
        self.assertEqual(p['HostConfig']['Mounts'][0]['Source'],'original-anonymous-volume')
        self.assertEqual(p['HostConfig']['PortBindings']['80/tcp'][0]['HostPort'],'32789')
        self.assertEqual(p['NetworkingConfig']['EndpointsConfig']['private']['IPAMConfig']['IPv4Address'],'172.25.0.4')

    async def test_autoremove_fails_before_stopping_old_container(self):
        self.fake.items['c-old']['HostConfig']['AutoRemove']=True
        rows=await self.engine.check(self.notify)
        self.assertEqual(rows[0]['status'],'error')
        self.assertTrue(self.fake.items['c-old']['State']['Running'])
        self.assertFalse(any(a[1]=='stop' for a in self.fake.calls))

    async def test_legacy_tasks_automatically_migrate(self):
        self.engine.save_tasks(['legacy:demo:latest'])
        restarted=Engine(self.cfg,Path(self.tmp.name),self.fake)
        self.assertEqual(restarted.tasks,['image:demo:latest'])

class DockerApiTransportTests(unittest.TestCase):
    def test_real_unix_http_helper_negotiates_api_and_sends_create_payload(self):
        import http.server
        import json
        import socketserver
        import subprocess
        import sys
        import threading
        requests=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                requests.append((self.path,None))
                data=json.dumps({'ApiVersion':'1.45'}).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path,body))
                data=b'{"Id":"created-id"}'
                self.send_response(201); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); path=root/'socket'
            try:
                server=socketserver.UnixStreamServer(str(path),Handler)
            except PermissionError:
                self.skipTest('当前执行环境禁止创建 Unix socket；需在 Linux CI/服务器运行该测试')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                body={'Image':'sha256:new','HostConfig':{'Memory':12345}}
                (root/'request.json').write_text(json.dumps(body))
                result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'docker_api.py'),
                                       'unix://'+str(path),'POST','/containers/create?name=web',str(root/'request.json')],
                                      capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout)['Id'],'created-id')
                self.assertEqual(requests,[('/version',None),('/v1.45/containers/create?name=web',body)])
            finally:
                server.shutdown();server.server_close();thread.join()
