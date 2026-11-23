"""Checks that adoption/recovery cannot mistake a similar image or volume for the original."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from zentaoctl_lib import site


class EvidenceTests(unittest.TestCase):
    def test_hardened_nginx_uses_the_last_configuration_replacement(self):
        history = ("COPY nginx.hardened.conf /etc/nginx/conf.d/default.conf # buildkit\n"
                   "COPY nginx.conf /etc/nginx/conf.d/default.conf # buildkit")
        self.assertEqual(site._target({"Config": {}}, history), "nginx-hardened")
        replaced = "COPY other.conf /etc/nginx/conf.d/default.conf # buildkit\n" + history
        self.assertIsNone(site._target({"Config": {}}, replaced))

    def test_mismatching_bind_source_on_named_volume_is_blocked(self):
        spec = {"type": "volume", "source": "mysql", "target": "/var/lib/mysql"}
        definitions = {"mysql": {"name": "alpha_mysql", "driver_opts":
                       {"device": "/original/mysql", "type": "none", "o": "bind"}}}
        volume = {"Driver": "local", "CreatedAt": "2020-11-23T00:00:00Z",
                  "Options": {"device": "/another/mysql", "type": "none", "o": "bind"},
                  "Labels": {site.LABEL + "project": "alpha", site.LABEL + "volume": "mysql"}}
        blockers = []
        with patch.object(site, "_inspect", return_value=volume):
            detail = site._volume_details(spec, definitions, {}, blockers, "mysql:/var/lib/mysql", "alpha")
        self.assertEqual(detail["source"], "/another/mysql")
        self.assertTrue(blockers)

    def test_missing_volume_is_not_synthesized(self):
        blockers = []
        with patch.object(site, "_inspect", return_value=None):
            site._volume_details({"type": "volume", "source": "db"}, {"db": {"name": "demo_db"}},
                                 {}, blockers, "mysql:/var/lib/mysql", "demo")
        self.assertIn("demo_db", blockers[0])

    def test_explicit_oneoff_jobs_not_counted_as_permanent_services(self):
        with patch.object(site, "_docker", return_value="alpha\tphp\tFalse\nalpha\tdebug\tTrue\n"):
            self.assertEqual(site.discover(), {"alpha": ["php"]})


if __name__ == "__main__":
    unittest.main()
