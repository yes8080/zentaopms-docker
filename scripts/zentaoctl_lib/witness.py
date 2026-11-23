"""Verify the data behind a Docker mount even when its old container is gone."""
import re

from .common import Error

MYSQL = "/var/lib/mysql"
CONFIG = "/var/lib/zentao/config"
DATA = "/var/www/zentao/www/data"
CHECKS = {"mysql": (MYSQL,), "php": (CONFIG, DATA)}
STAT = 'stat -c "%i" "$1"'
SERVER_ID = '''awk -F= '/^server-uuid=/{print $2}' "$1/auto.cnf"'''


def observe(ctx, report, svc, dest, script):
    service = report["services"].get(svc, {})
    if service.get("state") == "running":
        result = ctx.docker("exec", "--user", "0", service["id"], "sh", "-ec", script, "check", dest)
    else:
        mount = report["evidence"]["mounts"][svc][dest]
        source = mount["name"] if mount["type"] == "volume" else mount["source"]
        if not source or "," in source:
            raise Error(f"无法通过只读挂载核实 {svc} {dest}。")
        if mount["type"] == "volume":
            ctx.docker("volume", "inspect", "--format", "{{.Name}}", "--", source)
        # The smaller Nginx image contains stat and awk. No image pull or volume copy-up.
        options = f"type={mount['type']},src={source},dst=/witness,readonly"
        if mount["type"] == "volume":
            options += ",volume-nocopy"
        result = ctx.docker("run", "--pull", "never", "--rm", "--network", "none", "--read-only",
                            "--user", "0", "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE",
                            "--security-opt", "no-new-privileges", "--mount", options,
                            "--entrypoint", "sh", report["evidence"]["images"]["nginx"],
                            "-ec", script, "check", "/witness")
    return result.strip()


def capture(ctx, report, stage, *, include_backup=True):
    evidence = {}
    protected = dict(CHECKS)
    backup_path = "/var/lib/zentao/backup"
    if include_backup and backup_path in report["evidence"]["mounts"].get("php", {}):
        protected["php"] = (*protected["php"], backup_path)
    for svc, paths in protected.items():
        for dest in paths:
            inode = observe(ctx, report, svc, dest, STAT)
            if not inode.isdecimal():
                raise Error(f"无法核实 {svc} 的数据目录 {dest}。")
            evidence[dest] = inode
    server_id = observe(ctx, report, "mysql", MYSQL, SERVER_ID)
    if not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", server_id):
        raise Error("数据库卷缺少原 MySQL server UUID。请检查原数据或使用备份恢复。")
    if stage == "site":
        observe(ctx, report, "php", CONFIG, 'test -s "$1/my.php" && echo ok')
    evidence["mysql_uuid"] = server_id
    return evidence


def verify_identity(ctx, report):
    prior = ctx.record.get("witnesses") or {}
    expected_backup = "/var/lib/zentao/backup" in prior
    if not prior or capture(ctx, report, ctx.record["stage"], include_backup=expected_backup) != prior:
        raise Error("数据目录身份或 MySQL server UUID 与上次验收不符。请核实原卷和备份。")
