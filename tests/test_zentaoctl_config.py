"""Checks for the settings mistakes that caused the Redis and persistence incidents."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from zentaoctl_lib.common import Context, Error


@unittest.skipUnless(shutil.which("docker"), "requires the Compose client")
class ConfigSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("compose.yaml", "compose.redis.yaml"):
            (self.root / name).write_text("services: {}\n")
        self.env = self.root / ".env"
        self.engine = {"id": "fake-id", "context": "default", "endpoint": "unix:///fake"}

    def test_instance_file_wins_over_export_and_multiline_values(self):
        self.env.write_text("COMPOSE_FILE=compose.yaml:compose.redis.yaml\n"
                            "UNRELATED='first line\nCOMPOSE_FILE=other.yaml'\n"
                            "MYSQL_PASSWORD='one$two$$three'\n")
        with patch.dict(os.environ, COMPOSE_FILE="forgotten.yaml"):
            ctx = Context(self.root, "sample", self.env, self.engine)
            self.assertEqual(ctx.settings()["MYSQL_PASSWORD"], "one$two$$three")
            self.assertEqual([p.name for p in ctx.files()], ["compose.yaml", "compose.redis.yaml"])
        self.assertFalse(ctx.state_dir.exists())

    def test_lost_override_is_named(self):
        self.env.write_text("COMPOSE_FILE=compose.yaml:compose.hardened.yaml\n")
        ctx = Context(self.root, "sample", self.env, self.engine)
        with self.assertRaisesRegex(Error, "compose.hardened.yaml"):
            ctx.files()
        self.assertFalse(ctx.state_dir.exists())
