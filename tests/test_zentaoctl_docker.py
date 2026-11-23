"""Run with ZENTAOCTL_TEST_DOCKER=1 python3 -m unittest discover -s tests.

Uses small disposable containers to test Docker identities and storage, without
reading a local ZenTao database. Only resources in the random test project are removed.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from zentaoctl_lib import site

ALPINE = 'alpine@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf'

HTTP_RESPONDER = r"""#!/bin/sh
while IFS= read -r line; do
    [ "$line" = "$(printf '\r')" ] && break
done
printf 'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok'
"""


# Both synthetic application and database report the identity held in auto.cnf.
FIXTURE_UUID = 'd58f01a2-ce59-41e5-8efb-f031c90f2c54'
PHP_STUB = """#!/bin/sh
if test "$3" = identity; then
    echo '{"uuid":"FIXTURE_UUID","name":"zentao","prefix":"zt_"}'
    exit
fi
case "$2" in
*information_schema*)
    if test -s /var/lib/zentao/config/my.php; then echo version:22.7; else echo pending; fi;;
*scandir*) echo no;;
esac
""".replace('FIXTURE_UUID', FIXTURE_UUID)

MYSQL_SETUP = r"""cat > /usr/local/bin/mysql <<'SQL'
#!/bin/sh
case "$$*" in
*server_uuid*) printf '%s\tzentao\n' "$$(sed -n 's/^server-uuid=//p' /var/lib/mysql/auto.cnf)";;
esac
SQL
chmod +x /usr/local/bin/mysql
test -s /var/lib/mysql/auto.cnf || echo server-uuid=FIXTURE_UUID > /var/lib/mysql/auto.cnf
exec sleep 3600""".replace('FIXTURE_UUID', FIXTURE_UUID)


@unittest.skipUnless(os.environ.get('ZENTAOCTL_TEST_DOCKER') == '1', 'requires a local Docker engine')
class DockerSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory(prefix='zentaoctl-docker-test-')
        cls.addClassCleanup(cls.sandbox.cleanup)
        cls.root = Path(cls.sandbox.name)
        cls.root.chmod(0o755)  # Permit a rootless daemon with a different UID to traverse the path.
        cls.project = 'ztctltest' + secrets.token_hex(8)
        cls.secret = secrets.token_hex(16)
        cls.files = [cls.root / 'compose.yaml', cls.root / 'cache.yaml']
        base = {
            'services': {
                'mysql': {'volumes': ['db:/var/lib/mysql']},
                'php': {'volumes': ['config:/var/lib/zentao/config', 'data:/var/www/zentao/www/data']},
                'nginx': {'volumes': ['data:/var/www/zentao/www/data:ro']},
            },
            'volumes': {'db': {}, 'config': {}, 'data': {}},
        }
        for svc in base['services'].values():
            svc.update(image=ALPINE, command=['sleep', '3600'], stop_grace_period='1s')
        mysql = base['services']['mysql']
        mysql['environment'] = {'MYSQL_PASSWORD': '${MYSQL_PASSWORD:?}', 'MYSQL_DATABASE': 'zentao',
                                'MYSQL_USER': 'zentao'}
        mysql['command'] = ['sh', '-ec', MYSQL_SETUP]
        # Stub the application-level PHP responses. Docker, HTTP, volume metadata and
        # retained data are real, and the tests make no claims about a real ZenTao login.
        php = cls.root / 'php-fixture'
        php.write_text(PHP_STUB)
        php.chmod(0o755)
        base['services']['php']['volumes'].append(f'{php}:/usr/local/bin/php:ro')
        base['services']['php']['command'] = [
            'sh', '-ec', 'echo fixture > /var/lib/zentao/config/my.php; exec sleep 3600']
        with socket.socket() as port_socket:
            port_socket.bind(('127.0.0.1', 0))
            port = port_socket.getsockname()[1]
        base['services']['nginx']['ports'] = [f'127.0.0.1:{port}:80']
        responder = cls.root / 'http-fixture'
        responder.write_text(HTTP_RESPONDER)
        responder.chmod(0o755)
        base['services']['nginx']['volumes'].append(f'{responder}:/usr/local/bin/http-fixture:ro')
        base['services']['nginx']['command'] = ['nc', '-lk', '-p', '80', '-e', '/usr/local/bin/http-fixture']
        cache = {'services': {'redis': {'image': ALPINE, 'command': ['sleep', '3600'],
                                         'stop_grace_period': '1s'}}}
        for path, document in zip(cls.files, [base, cache]):
            path.write_text(json.dumps(document))
        shutil.copytree(ROOT / 'scripts', cls.root / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        cls.env = cls.root / '.env'
        cls.env.write_text(f'COMPOSE_FILE=compose.yaml:cache.yaml\nMYSQL_PASSWORD={cls.secret}\n')
        cls.env.chmod(0o600)
        cls.addClassCleanup(cls.compose, 'down', '--volumes', files=cls.files)
        cls.compose('up', '-d', '--wait', files=cls.files)

    @classmethod
    def compose(cls, *args, files):
        cmd = ['docker', 'compose', '--project-directory', str(cls.root), '-p', cls.project,
               '--env-file', str(cls.env)]
        for path in files:
            cmd += ['-f', str(path)]
        return subprocess.check_output(cmd + list(args), text=True, cwd=cls.root)

    def snapshot(self, files, previous=None):
        model = json.loads(self.compose('config', '--format', 'json', files=files))
        return site.inspect_site(self.project, model, files, self.root, previous=previous)

    def cli(self, action, *args):
        return subprocess.run([str(self.root / 'scripts/zentaoctl'), action, '--project', self.project,
                               '--env-file', str(self.env), *args], capture_output=True, text=True)

    def ids(self):
        return {k: v['id'] for k, v in self.snapshot(self.files)['services'].items()}

    def checked_cli(self, action, *args):
        reply = self.cli(action, *args)
        self.assertNotIn(self.secret, reply.stdout + reply.stderr)
        self.assertEqual(reply.returncode, 0, reply.stderr)
        return reply.stdout

    def test_cli_lifecycle_and_database_identity(self):
        original = self.ids()
        self.checked_cli('adopt', '--dry-run')
        self.assertFalse((self.root / '.zentaoctl').exists())
        self.checked_cli('adopt')
        self.checked_cli('status', '--verify')
        self.assertIn('0', self.checked_cli('recover', '--dry-run'))
        self.checked_cli('recover')
        self.assertEqual(original, self.ids())

        # The application can point at a different database even with all volumes intact.
        probe = self.root / 'php-fixture'
        probe.write_text(PHP_STUB.replace('"name":"zentao"', '"name":"other-fixture-db"'))
        try:
            reply = self.cli('status', '--verify')
            self.assertNotEqual(reply.returncode, 0)
            self.assertIn('数据库', reply.stderr)
            self.assertEqual(self.ids(), original)
        finally:
            probe.write_text(PHP_STUB)

        self.checked_cli('stop')
        self.assertTrue(all(s['state'] == 'exited' for s in self.snapshot(self.files)['services'].values()))
        self.checked_cli('recover')
        self.checked_cli('restart')
        self.assertEqual(original, self.ids())

        self.compose('exec', '-T', 'php', 'sh', '-ec',
                     'echo attachment > /var/www/zentao/www/data/test-attachment', files=self.files)
        self.compose('down', files=self.files)
        self.checked_cli('recover')
        self.assertFalse(set(original.values()) & set(self.ids().values()))
        self.assertEqual(self.compose('exec', '-T', 'php', 'cat',
                                     '/var/www/zentao/www/data/test-attachment', files=self.files).strip(),
                         'attachment')
        self.compose('exec', '-T', 'mysql', 'sh', '-ec',
                     'sed "s/^/server-uuid=/" /proc/sys/kernel/random/uuid > /var/lib/mysql/auto.cnf',
                     files=self.files)
        before = self.ids()
        changed = self.cli('recover')
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn('UUID', changed.stderr)
        self.assertEqual(before, self.ids())

        self.compose('down', files=self.files)
        db = self.project + '_db'
        subprocess.run(['docker', 'volume', 'rm', db], check=True, capture_output=True)
        try:
            missing = self.cli('recover')
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn(db, missing.stderr)
            self.assertNotEqual(subprocess.run(['docker', 'volume', 'inspect', db], capture_output=True).returncode, 0)
        finally:
            self.compose('up', '-d', '--wait', files=self.files)

    def test_inventory_and_dropped_redis(self):
        full = self.snapshot(self.files)
        self.assertFalse(full['blockers'], full['blockers'])
        self.assertFalse(full['missing'])
        self.assertEqual(sorted(full['services']), ['mysql', 'nginx', 'php', 'redis'])
        dropped = self.snapshot(self.files[:1], full['evidence'])
        self.assertTrue(any('redis' in b for b in dropped['blockers']), dropped)
        self.assertNotIn(self.secret, json.dumps(dropped))
        self.assertEqual(full['services']['redis']['id'], dropped['services']['redis']['id'])

    def test_four_services_with_wrong_database_volume(self):
        original = self.snapshot(self.files)
        override = self.root / 'other-db.yaml'
        override.write_text(json.dumps({
            'services': {'mysql': {'volumes': ['different-db:/var/lib/mysql']}},
            'volumes': {'different-db': {}},
        }))
        wrong = self.snapshot(self.files + [override], original['evidence'])
        self.assertEqual(set(original['services']), set(wrong['services']))
        self.assertTrue(any('/var/lib/mysql' in b for b in wrong['blockers']), wrong)
        volumes = subprocess.check_output(['docker', 'volume', 'ls', '-q', '--filter',
                                          'label=com.docker.compose.project=' + self.project], text=True)
        self.assertNotIn(self.project + '_different-db', volumes.splitlines())

    def test_stopped_container_is_still_identified(self):
        original = self.snapshot(self.files)
        self.compose('stop', '-t', '1', 'nginx', files=self.files)
        try:
            stopped = self.snapshot(self.files, original['evidence'])
            self.assertFalse(stopped['blockers'], stopped['blockers'])
            self.assertEqual(stopped['services']['nginx']['state'], 'exited')
            self.assertEqual(stopped['services']['nginx']['id'], original['services']['nginx']['id'])
            self.assertIn('nginx', site.discover()[self.project])
        finally:
            self.compose('start', 'nginx', files=self.files)


if __name__ == '__main__':
    unittest.main()
