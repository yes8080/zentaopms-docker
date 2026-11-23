import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from zentaoctl_lib import dataops as ops
from zentaoctl_lib.common import Error


class DataOperations(unittest.TestCase):
    def test_restored_endpoints_and_dollar_literal(self):
        runtime = {
            "mysql": {}, "php": {"environment": {"DOLLAR": "$1x"}, "read_only": True},
            "nginx": {}, "redis": {"command": ["echo \"$ZT_REDIS_PASSWORD\""]},
        }
        spec = ops._isolated_model(runtime, dict.fromkeys(runtime, "sha256:" + 64 * "a"),
                                   {r: "isolated_" + r for r in ops.ROLES}, 19091)
        self.assertTrue(spec["networks"]["sandbox"]["internal"])
        self.assertEqual(spec["services"]["nginx"]["ports"], ["127.0.0.1:19091:80"])
        for name in ["php", "mysql", "redis"]:
            self.assertEqual(spec["services"][name]["networks"], ["sandbox"])
            self.assertNotIn("ports", spec["services"][name])
        self.assertEqual(spec["services"]["php"]["environment"]["DOLLAR"], "$$1x")
        self.assertEqual(spec["services"]["redis"]["command"], ['echo "$$ZT_REDIS_PASSWORD"'])
        self.assertIn("config:/var/lib/zentao/config:ro", spec["services"]["php"]["volumes"])
        self.assertIn("--event-scheduler=OFF", spec["services"]["mysql"]["command"])

    def test_backup_tamper_detected_before_restore(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)
            names = [f"{role}.tar.gz" for role in ops.ROLES] + ["deployment.tar.gz","runtime.json"]
            for name in names:
                (archive / name).write_bytes(b"test contents")
            (archive / "manifest.json").write_text(json.dumps({
                "format": 1, "project": "demo", "files": {n: ops._sha(archive / n) for n in names},
                "database": {"name": "demo", "prefix": "zt_", "uuid": "fc443399-6870-49a2-b8e8-da893405e716"},
                "images": dict.fromkeys(("php","mysql","nginx"), "sha256:" + "a" * 64),
            }))
            ops.validate_backup(archive)
            (archive / "config.tar.gz").write_bytes(b"modified")
            with self.assertRaisesRegex(Error, "摘要错误"):
                ops.validate_backup(archive)

    def test_purge_skips_external_volume_and_protected_backup(self):
        base = {"type": "volume", "driver": "local", "read_only": False, "source": "", "device": ""}
        evidence = {"mounts": {"mysql": {"/var/lib/mysql": dict(base,name="shared")},
                               "php": {"/var/lib/zentao/config": dict(base,name="backup"),
                                       "/var/www/zentao/www/data": dict(base,name="misc", source="/data"),
                                       "/var/lib/zentao/backup": dict(base,name="backup")}}}
        ctx = SimpleNamespace(record={"evidence": evidence, "files": [], "signature": "example"},
                              project="demo", files=lambda: [],
                              signature=lambda files: "example",
                              model=lambda: {"volumes": {"db": {"name": "shared", "external": True}}})
        options = SimpleNamespace(confirm_project="demo", confirm_volume=["shared"], dry_run=False)
        with patch.object(ops,"docker",return_value="") as docker:
            with self.assertRaisesRegex(Error, "逐个指定"):
                ops.purge(ctx,options)
            self.assertFalse(any("rm" in item.args for item in docker.call_args_list))

    def test_rejects_writing_backup_under_bind_root(self):
        with self.assertRaisesRegex(Error, "数据目录"):
            ops._allowed_backup_location(Path("/data/zentao/backups/today"),
                                         {"mounts": {"db": {"/var/lib/mysql":{"source": "/data/zentao/mysql"}}}})
