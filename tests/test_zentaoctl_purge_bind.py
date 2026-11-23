import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from zentaoctl_lib import purge_bind as pb
from zentaoctl_lib.common import Error


class PurgeBindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.role = base / "site" / "data"
        self.role.mkdir(parents=True)
        self.sample = self.role / "attachment"
        self.sample.write_text("sample")
        self.backup = base / "backup"
        self.backup.mkdir()
        self.proof = {"/var/www/zentao/www/data": str(self.role.stat().st_ino), "mysql_uuid": "original"}
        self.record = {"witnesses": self.proof, "evidence": {"mounts": {}}, "engine": {"id": "engine"}}
        self.ctx = SimpleNamespace(project="demo", engine={"endpoint": "unix:///var/run/docker.sock"},
                                   root=base / "deployment", state_dir=base / "deployment/state",
                                   env_file=base / "deployment/env", record=self.record)
        self.ctx.docker = Mock(return_value="/var/lib/docker")
        self.args = SimpleNamespace(confirm_dir=[str(self.role)], confirm_volume=[], confirm_project="demo",
                                    from_backup=str(self.backup), dry_run=False)
        self.vol = {"name": "demo_host-data", "created": "then"}
        self.candidates = {str(self.role): ("data", "/var/www/zentao/www/data", self.vol, "host-data")}
        self.manifest = {"project": "demo", "site": self.record}

    def run_purge(self, *, inventory=None, host=None):
        with patch.object(pb, "_candidates", return_value=self.candidates), \
             patch.object(pb, "_inventory", side_effect=inventory), \
             patch.object(pb, "_verify_host", side_effect=host), \
             patch.object(pb, "validate_backup", return_value=self.manifest):
            return pb.purge_bind(self.ctx, self.args)

    def test_confirmed_role_only_and_dry_run(self):
        neighbour = self.role.parent / "keep"
        neighbour.write_text("keep")
        self.args.dry_run = True
        self.run_purge()
        self.assertTrue(self.sample.exists())
        self.args.dry_run = False
        self.run_purge()
        self.assertFalse(self.role.exists())
        self.assertTrue(neighbour.is_file())
        self.assertTrue(self.role.parent.is_dir())

    def test_ancestor_or_wrong_backup_rejected_before_removal(self):
        self.args.confirm_dir = [str(self.role.parent)]
        with self.assertRaises(Error):
            self.run_purge()
        self.args.confirm_dir = [str(self.role)]
        self.manifest["project"] = "another"
        with self.assertRaises(Error):
            self.run_purge()
        self.assertTrue(self.sample.is_file())

    def test_nested_symlink_or_mount_is_kept(self):
        (self.role / "linked").symlink_to(self.backup, target_is_directory=True)
        with self.assertRaisesRegex(Error, "符号链接"):
            self.run_purge()
        (self.role / "linked").unlink()
        original = pb._mount_id
        inode = self.sample.stat().st_ino
        with patch.object(pb, "_mount_id", side_effect=lambda fd: "nested" if os.fstat(fd).st_ino == inode else original(fd)):
            with self.assertRaisesRegex(Error, "嵌套挂载"):
                self.run_purge()
        self.assertTrue(self.sample.exists())

    def test_backup_inside_role_or_missing_confirmation_cannot_delete(self):
        nested = self.role / "saved"
        nested.mkdir()
        self.args.from_backup = str(nested)
        with self.assertRaisesRegex(Error, "备份"):
            self.run_purge()
        self.args.from_backup = str(self.backup)
        self.args.confirm_project = None
        with self.assertRaisesRegex(Error, "confirm-project"):
            self.run_purge()
        self.assertTrue(self.sample.exists())

    def test_unproven_host_keeps_all_files(self):
        with self.assertRaisesRegex(Error, "UUID"):
            self.run_purge(host=Error("UUID changed"))
        self.assertTrue(self.sample.exists())

    def test_shared_volume_or_direct_bind_is_retained(self):
        volume = {"Name": self.vol["name"], "Driver": "local", "CreatedAt": self.vol["created"],
                  "Labels": {"com.docker.compose.project": "demo", "com.docker.compose.volume": "host-data"},
                  "Options": {"o": "bind", "type": "none", "device": str(self.role)}}
        other = {**volume, "Name": "other_data", "Options": {**volume["Options"], "device": str(self.role.parent)}}
        self.ctx.docker = Mock(side_effect=["", "names", json.dumps([volume, other])])
        with self.assertRaisesRegex(Error, "另一个 Docker 卷"):
            pb._inventory(self.ctx, self.candidates, [str(self.role)])
        self.ctx.docker = Mock(side_effect=["container", 'other|' + json.dumps([{"Type": "bind", "Source": str(self.role)}]),
                                           "names", json.dumps([volume])])
        with self.assertRaisesRegex(Error, "容器挂载"):
            pb._inventory(self.ctx, self.candidates, [str(self.role)])

    def test_mount_alias_maps_to_same_filesystem(self):
        table = [(Path("/"), "7:1", Path("/")), (Path("/alias"), "7:1", Path("/real"))]
        self.assertTrue(pb._same_storage(Path("/alias"), Path("/real/mysql"), table))
        self.assertFalse(pb._same_storage(Path("/else"), Path("/real/mysql"), table))
