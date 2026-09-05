"""Exact bash process-substitution entry, with GitHub transport fixtures."""
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.root/'commit.json').write_text(json.dumps({'sha': SHA}))
        curl = self.bin/'curl'
        curl.write_text('''#!/usr/bin/env python3
import os,sys,shutil
from pathlib import Path
root=Path(os.environ['FIXTURE_ROOT'])
args=sys.argv[1:]
url=next(x for x in args if x.startswith('https://'))
with (root/'requests.log').open('a') as f: f.write(url+'\\n')
if os.environ.get('FAIL_FETCH')=='1': sys.exit(22)
source='commit.json' if 'api.github.com' in url else 'source.tar.gz'
shutil.copyfile(root/source,args[args.index('-o')+1])
''')
        curl.chmod(0o755)
        self.env = dict(os.environ, FIXTURE_ROOT=str(self.root), PATH=str(self.bin)+os.pathsep+os.environ['PATH'])

    def archive(self, missing=False, traversal=False):
        with tarfile.open(self.root/'source.tar.gz','w:gz') as t:
            for name in ('install.sh','uninstall.sh','core.py','container_update.py','docker_api.py','admin.py','dependency_env.py','autoupdate_bot.py','requirements.txt','requirements.lock'):
                if missing and name=='install.sh':
                    continue
                t.add(ROOT/name, arcname='repo-'+SHA+'/'+name)
            if traversal:
                entry=tarfile.TarInfo('repo-'+SHA+'/../escape')
                entry.size=1
                t.addfile(entry,io.BytesIO(b'x'))

    def launch(self):
        # Same /dev/fd execution mode as bash <(curl ...); only transport is replaced.
        return subprocess.run(['bash','-c','bash <(cat "$1") --download-only "$2"',
                               '--',str(ROOT/'deploy.sh'),str(self.root/'download')],
                              env=self.env,text=True,capture_output=True,timeout=20)

    def test_process_substitution_downloads_pinned_complete_project(self):
        self.archive()
        result=self.launch()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'download/.source-commit').read_text().strip(),SHA)
        self.assertTrue((self.root/'download/install.sh').is_file())
        self.assertIn('/tar.gz/'+SHA,(self.root/'requests.log').read_text())

    def test_old_incomplete_repository_rejected(self):
        self.archive(missing=True)
        result=self.launch()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'download').exists())

    def test_archive_traversal_rejected(self):
        self.archive(traversal=True)
        result=self.launch()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'download').exists())

    def test_download_failure_does_not_create_destination(self):
        self.env['FAIL_FETCH']='1'
        result=self.launch()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'download').exists())
