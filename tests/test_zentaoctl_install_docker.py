"""Managed installation and hardening with real Docker and synthetic application responses."""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import unittest

from test_zentaoctl_docker import ALPINE, HTTP_RESPONDER, MYSQL_SETUP, PHP_STUB, ROOT


@unittest.skipUnless(os.environ.get('ZENTAOCTL_TEST_DOCKER') == '1', 'requires a local Docker engine')
class ManagedInstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='zentaoctl-install-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = 'ztctlinstall' + secrets.token_hex(8)
        self.env = self.root / 'site.env'
        self.dc = ['docker', 'compose', '-p', self.project, '--project-directory', str(self.root),
                   '--env-file', str(self.env)]
        self.addCleanup(self.cleanup_docker)
        shutil.copytree(ROOT / 'scripts', self.root / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        self.build = self.root / 'build'
        self.build.mkdir()
        (self.build / 'http').write_text(HTTP_RESPONDER)
        (self.build / 'php').write_text(PHP_STUB)
        (self.build / 'Dockerfile').write_text(f'''FROM {ALPINE} AS php
COPY ready /fixture-ready
COPY --chmod=755 php /usr/local/bin/php
RUN mkdir -p /var/www/zentao/www && echo 22.7 > /var/www/zentao/VERSION && touch /var/www/zentao/www/install.php /var/www/zentao/www/upgrade.php
CMD ["sleep","3600"]
FROM {ALPINE} AS nginx
COPY --chmod=755 http /usr/local/bin/responder
CMD ["nc","-lk","-p","80","-e","/usr/local/bin/responder"]
FROM php AS php-hardened
RUN rm /var/www/zentao/www/install.php /var/www/zentao/www/upgrade.php
FROM nginx AS nginx-hardened
''')
        services = {
            'mysql': {'image': ALPINE, 'volumes': ['mysql-data:/var/lib/mysql'],
                      'environment': {'MYSQL_PASSWORD': '${MYSQL_PASSWORD:?}', 'MYSQL_USER': 'zentao',
                                      'MYSQL_DATABASE': 'zentao'}, 'command': ['sh', '-ec', MYSQL_SETUP]},
            'php': {'build': {'context': './build', 'target': 'php'},
                    'environment': {'ZT_DB_PREFIX': 'zt_'},
                    'volumes': ['app-config:/var/lib/zentao/config', 'app-data:/var/www/zentao/www/data']},
            'nginx': {'build': {'context': './build', 'target': 'nginx'},
                      'volumes': ['app-data:/var/www/zentao/www/data:ro'], 'ports': ['127.0.0.1:${ZENTAO_PORT}:80']},
        }
        for service in services.values():
            service['stop_grace_period'] = '1s'
        (self.root / 'compose.yaml').write_text(json.dumps(
            {'services': services, 'volumes': {'mysql-data': {}, 'app-config': {}, 'app-data': {}}}))
        hardened = {'services': {
            'php': {'read_only': True, 'user': '33:33', 'build': {'target': 'php-hardened'},
                    'volumes': ['app-config:/var/lib/zentao/config:ro', 'app-backup:/var/lib/zentao/backup']},
            'nginx': {'read_only': True, 'build': {'target': 'nginx-hardened'}},
        }, 'volumes': {'app-backup': {}}}
        (self.root / 'compose.hardened.yaml').write_text(json.dumps(hardened))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = str(sock.getsockname()[1])
        self.state = self.root / '.zentaoctl' / self.project

    def cleanup_docker(self):
        if self.env.exists():
            subprocess.run(self.dc + ['down', '--volumes'], check=True, capture_output=True)
        references = subprocess.check_output(['docker', 'image', 'ls', '--format',
                                              '{{.Repository}}:{{.Tag}}'], text=True).splitlines()
        own_refs = [ref for ref in references if ref.startswith('zentaoctl/' + self.project + '-')]
        if own_refs:
            subprocess.run(['docker', 'image', 'rm', *own_refs], check=True, capture_output=True)

    def cli(self, action, *args, success=True):
        proc = subprocess.run([str(self.root / 'scripts/zentaoctl'), action, '--project', self.project,
                               '--env-file', str(self.env), *args], capture_output=True, text=True)
        self.assertEqual(proc.returncode == 0, success, proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def release(self):
        return json.loads((self.state / 'release.json').read_text())

    def test_failed_build_resume_and_harden(self):
        install = ['--port', self.port]
        self.cli('install', *install, '--dry-run')
        self.assertFalse(self.env.exists())
        # Missing COPY input fails before any database volume can be initialized.
        self.cli('install', *install, success=False)
        self.assertEqual(json.loads((self.state / 'operation.json').read_text())['phase'], 'build')
        vols = subprocess.check_output(['docker', 'volume', 'ls', '-q', '--filter',
                                       'label=com.docker.compose.project=' + self.project], text=True)
        self.assertFalse(vols.strip())
        (self.build / 'ready').touch()
        self.cli('install', *install)
        before = self.release()
        self.assertEqual(before['stage'], 'install')
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.cli('install', *install)
        self.assertEqual(self.release()['evidence'], before['evidence'])
        self.cli('harden', success=False)

        subprocess.run(self.dc + ['exec', '-T', 'php', 'sh', '-ec',
                                  'echo fixture > /var/lib/zentao/config/my.php; '
                                  'echo attachment > /var/www/zentao/www/data/sample'], check=True)
        self.cli('install', *install)
        self.assertEqual(self.release()['stage'], 'site')
        self.cli('harden', '--dry-run')
        self.assertEqual(self.release()['evidence'], before['evidence'])
        self.cli('harden')
        hardened = self.release()['evidence']
        self.assertEqual(hardened['targets']['php'], 'php-hardened')
        self.assertEqual(hardened['targets']['nginx'], 'nginx-hardened')
        for svc in ('php', 'nginx'):
            self.assertNotEqual(hardened['images'][svc], before['evidence']['images'][svc])
        self.cli('recover')
        data = subprocess.check_output(self.dc + ['exec', '-T', 'php', 'cat',
                                                  '/var/www/zentao/www/data/sample'], text=True)
        self.assertEqual(data.strip(), 'attachment')
