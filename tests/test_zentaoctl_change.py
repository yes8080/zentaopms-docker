"""Exercise project-preserving configuration changes using isolated Compose resources."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from zentaoctl_lib import change, site
from zentaoctl_lib.cli import parser
from zentaoctl_lib.common import Context, Error

ALPINE = "alpine@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf"


def spare_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ChangeTests(unittest.TestCase):
    def test_component_switch_preserves_operator_overlays(self):
        root = Path("/test")
        ctx = type("Context", (), {"root": root})()
        original = [root / f for f in ("compose.yaml", "compose.persistence.yaml", "compose.redis.yaml",
                                        "compose.hardened.yaml", "site-extra.yaml")]
        changed = change._cache_files(ctx, original, "apcu")
        self.assertEqual(changed, [*original[:2], root / "compose.apcu.yaml", *original[3:]])
        self.assertEqual(change._cache_files(ctx, original, "none"), original[:2] + original[3:])

    def test_backing_device_is_part_of_storage_comparison(self):
        model = {"services": {"mysql": {"volumes": [{"source": "db", "target": "/var/lib/mysql"}]}},
                 "volumes": {"db": {"driver_opts": {"device": "/old/mysql"}}}}
        other = json.loads(json.dumps(model))
        other["volumes"]["db"]["driver_opts"]["device"] = "/new/mysql"
        self.assertNotEqual(change._storage(model), change._storage(other))

    def test_online_upgrade_rejects_different_database_version(self):
        root = Path("/fixture/zentaopms-docker")
        ctx = type("Context", (), {"root": root})()
        app = root.parent / "zentaopms"
        model = {"services": {n: {"build": {"context": str(app), "target": n}} for n in ("php", "nginx")}}
        args = parser().parse_args(["update", "--backup-dir", "/tmp/backup-fixture"])
        with patch.object(change, "_checkout", return_value="a" * 40), patch.object(
                change, "_git", return_value="$config->version = '22.7';"), patch(
                    "zentaoctl_lib.install.installed_version", return_value="21.0"), self.assertRaisesRegex(
                        Error, "数据库版本"):
            change._check_build(ctx, model, args, ctx)

    def test_reinstall_rejects_overlapping_source_and_target(self):
        ctx = type("Context", (), {"project": "original", "env_file": Path("/old/.env")})()
        args = parser().parse_args(["reinstall", "--project", "original", "--target-project", "second",
                                    "--port", "18800", "--data-dir", "/fixture/new-data",
                                    "--backup-dir", "/fixture/new-data/backups",
                                    "--target-env-file", "/fixture/second.env"])
        with self.assertRaisesRegex(Error, "独立路径"):
            change.handle(ctx, args)

    @unittest.skipUnless(os.environ.get("ZENTAOCTL_TEST_DOCKER") == "1", "requires Docker Compose")
    def test_update_reconstructs_only_the_accepted_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = Context(root, "oldrelease", root / ".env", {"id": "test"})
            ctx.env_file.write_text("TEST_SECRET=first\n")
            ctx.state_dir.mkdir(parents=True)
            (ctx.state_dir / "config.key").write_text(secrets.token_hex(32))
            recipe = root / "compose.yaml"
            content = 'services:\n  php:\n    image: alpine:3.21\n    environment:\n      SECRET: ${TEST_SECRET}\n'
            recipe.write_text(content)
            git = ["git", "-C", str(root), "-c", "user.email=test@example.invalid", "-c", "user.name=fixture"]
            subprocess.run(git + ["init", "--quiet"], check=True)
            subprocess.run(git + ["add", "compose.yaml"], check=True)
            subprocess.run(git + ["commit", "-qm", "original"], check=True)
            rev = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
            ctx.record = {"compose_revision": rev, "signature": ctx.signature([recipe]), "files": [str(recipe)]}
            recipe.write_text(content.replace("3.21", "3.22"))
            with change._baseline(ctx, "update") as previous:
                self.assertEqual(previous.model()["services"]["php"]["image"], "alpine:3.21")
                self.assertEqual(ctx.model()["services"]["php"]["image"], "alpine:3.22")
            ctx.env_file.write_text("TEST_SECRET=changed\n")
            with self.assertRaisesRegex(Error, "无法重现"):
                with change._baseline(ctx, "update"):
                    self.fail("unexpectedly accepted changed credentials")

    @unittest.skipUnless(os.environ.get("ZENTAOCTL_TEST_DOCKER") == "1", "requires a local Docker engine")
    def test_configure_resumes_after_http_failure_without_replacing_data_services(self):
        temp = tempfile.TemporaryDirectory(prefix="zentaoctl-port-test-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        root.chmod(0o755)
        project = "ztport" + secrets.token_hex(8)
        env_file = root / ".env"
        port = spare_port()
        env_file.write_text(f"ZENTAO_PORT={port}\n")
        ctx = Context(root, project, env_file, site.engine_identity())
        services = {svc: {"image": ALPINE, "command": ["sleep", "3600"]} for svc in ("mysql", "php", "nginx")}
        services["mysql"]["volumes"] = ["db:/var/lib/mysql"]
        services["mysql"]["command"] = ["sh", "-ec", "echo server-uuid=11111111-2222-3333-4444-555555555555 > /var/lib/mysql/auto.cnf; exec sleep 3600"]
        services["php"]["volumes"] = ["cfg:/var/lib/zentao/config", "data:/var/www/zentao/www/data"]
        services["php"]["command"] = ["sh", "-ec", "echo fixture > /var/lib/zentao/config/my.php; exec sleep 3600"]
        services["nginx"].update(ports=["127.0.0.1:${ZENTAO_PORT}:80"], volumes=["data:/var/www/zentao/www/data:ro"])
        (root / "compose.yaml").write_text(json.dumps({"services": services, "volumes": {"db": {}, "cfg": {}, "data": {}}}))
        self.addCleanup(ctx.dc, "down", "-t", "1", "--volumes")
        ctx.dc("up", "-d", "--pull", "never", "--wait")
        original = ctx.snapshot()
        ctx.save(original)
        target = spare_port()
        args = parser().parse_args(["configure", "--project", project, "--port", str(target)])
        args.dry_run = True
        change.handle(ctx, args)
        self.assertIn(str(port), env_file.read_text())
        self.assertFalse((ctx.state_dir / "operation.json").exists())
        args.dry_run = False
        with patch.object(ctx, "verify", side_effect=Error("test HTTP failure")), self.assertRaises(Error):
            change.handle(ctx, args)
        self.assertTrue((ctx.state_dir / "operation.json").exists())
        replaced = ctx.snapshot()
        with patch.object(ctx, "verify") as verify:
            change.handle(ctx, args)
            verify.assert_called_once()
        self.assertFalse((ctx.state_dir / "operation.json").exists())
        final = ctx.require_site()
        self.assertNotEqual(original["services"]["nginx"]["id"], final["services"]["nginx"]["id"])
        self.assertEqual(replaced["services"]["nginx"]["id"], final["services"]["nginx"]["id"])
        for name in ("mysql", "php"):
            self.assertEqual(original["services"][name]["id"], final["services"][name]["id"])
        self.assertEqual(original["evidence"]["mounts"], final["evidence"]["mounts"])
        self.assertEqual(ctx.url(), f"http://127.0.0.1:{target}")


if __name__ == "__main__":
    unittest.main()
