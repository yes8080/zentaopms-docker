"""Offline data snapshots. Archive contents and Docker inspection never go to the console."""

import contextlib
import shlex
import gzip
import hashlib
import tarfile
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .common import Error, atomic_write, project_lock

ROLES = {
    "mysql": ("mysql", "/var/lib/mysql"),
    "config": ("php", "/var/lib/zentao/config"),
    "data": ("php", "/var/www/zentao/www/data"),
    "backup": ("php", "/var/lib/zentao/backup"),
}
PROJECT_LABEL = "com.docker.compose.project"
RESTORE_LABEL = "io.zentao.restore"
PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
IMAGE_RE = re.compile(r"sha256:[a-f0-9]{64}\Z")


def docker(ctx, *args, stdin=None, stdout=None):
    result = subprocess.run(["docker", *args], env=ctx.environ, stdin=stdin,
                            stdout=subprocess.PIPE if stdout is None else stdout,
                            stderr=subprocess.PIPE)
    if result.returncode:
        raise Error(f"Docker {args[0]} 失败。请检查项目状态、镜像、空间及 Docker 权限。")
    return result.stdout.decode().strip() if result.stdout is not None else ""


def _ids(ctx, kind, project):
    cmd = {"container": ["ps", "-aq"], "volume": ["volume", "ls", "-q"],
           "network": ["network", "ls", "-q"]}[kind]
    return docker(ctx, *cmd, "--filter", f"label={PROJECT_LABEL}={project}").splitlines()


def _sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _copy_archive(ctx, args, path):
    # Stream the archive. Piping into a ZipFile directly would use a file descriptor
    # behind the compressor and leave uncompressed bytes on disk.
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as file, gzip.GzipFile(fileobj=file, mode="wb", compresslevel=1) as compressed:
        with subprocess.Popen(["docker", *args], env=ctx.environ, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as proc:
            try:
                shutil.copyfileobj(proc.stdout, compressed, length=1024 * 1024)
            finally:
                proc.stdout.close()
            if proc.wait():
                raise Error(f"归档 {path.name} 失败，备份未完成。")


def validate_backup(archive):
    archive = Path(archive)
    if not archive.is_dir():
        raise Error("找不到备份目录。")
    try:
        document = json.loads((archive / "manifest.json").read_text())
        identity = document["database"]
        if (not PROJECT_RE.fullmatch(document["project"]) or not identity["name"] or
                not re.fullmatch("[A-Za-z0-9_]*", identity["prefix"]) or not re.fullmatch(
                    "[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", identity["uuid"])):
            raise ValueError
        if document["format"] != 1 or set(document["files"]) != {
                "mysql.tar.gz", "config.tar.gz", "data.tar.gz", "backup.tar.gz", "runtime.json", "deployment.tar.gz"}:
            raise ValueError
        for filename, checksum in document["files"].items():
            path = archive / filename
            if path.is_symlink() or not path.is_file() or _sha(path) != checksum:
                raise Error(f"备份文件 {filename} 缺失或摘要错误。")
        if set(document["images"]) not in ({"mysql", "php", "nginx"}, {"mysql", "php", "nginx", "redis"}):
            raise ValueError
        if not all(IMAGE_RE.fullmatch(v) for v in document["images"].values()):
            raise ValueError
    except (TypeError, KeyError, ValueError, OSError) as exc:
        raise Error("无有效备份清单。请使用本工具成功完成的备份。") from exc
    return document


def _allowed_backup_location(output, evidence):
    for service in evidence["mounts"].values():
        for mount in service.values():
            source = mount.get("source")
            if source and Path(source).is_absolute():
                root = Path(source).resolve()
                if root.name in {"mysql", "config", "data"}:
                    root = root.parent
                if output == root or root in output.parents:
                    raise Error("备份目标位于站点数据目录中，请选择独立目录。")


RUNTIME_FIELDS = ("environment", "command", "entrypoint", "user", "read_only", "cap_drop",
                  "cap_add", "security_opt", "pids_limit", "mem_limit", "healthcheck", "tmpfs", "depends_on")


def _runtime(ctx, model, states):
    runtime = {}
    for name, service in model["services"].items():
        settings = {k: service[k] for k in RUNTIME_FIELDS if k in service}
        actual = json.loads(docker(ctx, "container", "inspect", states[name]["id"]))[0]["Config"]
        env = dict(item.split("=", 1) for item in actual["Env"] if "=" in item)
        settings["environment"] = {key: env[key] for key in service.get("environment", {}) if key in env}
        # Rendered Compose commands can still contain '$$' for round-tripping.
        # The container carries the exact literals accepted by its shell.
        for key, inspect_key in (("command", "Cmd"), ("entrypoint", "Entrypoint")):
            if key in settings:
                settings[key] = actual[inspect_key]
        if "healthcheck" in settings:
            settings["healthcheck"] = dict(settings["healthcheck"], test=actual["Healthcheck"]["Test"])
        runtime[name] = settings
    return runtime


def _deployment_archive(ctx, path):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as file, tarfile.open(
            fileobj=file, mode="w:gz") as files:
        members = [(ctx.env_file, "environment"), (ctx.state_dir / "release.json", "release.json"),
                   (ctx.state_dir / "config.key", "config.key")]
        overrides = getattr(ctx, "overrides", {})
        members.extend((overrides.get(p, p), f"compose/{i}-{p.name}") for i, p in enumerate(ctx.files()))
        for source, name in members:
            if not source.is_file():
                raise Error(f"部署登记文件缺失：{source}")
            files.add(source.resolve(), arcname=name, recursive=False)


def _local_db_identity(ctx, mysql_id):
    from .install import database_identity
    actual = database_identity(ctx)
    local = docker(ctx, "exec", mysql_id, "sh", "-ec",
                   'export MYSQL_PWD="$MYSQL_PASSWORD"; exec mysql --batch --raw --skip-column-names '
                   '-u "$MYSQL_USER" --database="$MYSQL_DATABASE" -e "SELECT @@server_uuid, DATABASE()"')
    if local.split("\t") != [actual["uuid"], actual["name"]]:
        raise Error("PHP 实际使用的数据库与本项目 MySQL 卷不同，已停止备份操作。")
    return actual


def _backup_directory(ctx, mysql_id, prefix):
    # The application permits a custom built-in backup directory in its database.
    # Find it before stopping PHP, since older images store backups in tmpfs.
    if not re.fullmatch(r"[A-Za-z0-9_]*", prefix):
        raise Error("无法核实禅道配置表前缀。")
    sql = f"SELECT value FROM `{prefix}config` WHERE owner='system' AND module='backup' AND `key`='settingDir' AND value<>''"
    raw = docker(ctx, "exec", mysql_id, "sh", "-ec",
                 'export MYSQL_PWD="$MYSQL_PASSWORD"; exec mysql --batch --raw --skip-column-names '
                 '-u "$MYSQL_USER" --database="$MYSQL_DATABASE" --execute="$1"', "sh", sql)
    if not raw:
        return "/var/www/zentao/tmp/backup"
    if not raw.startswith("/") or len(raw.splitlines()) != 1 or Path(raw) == Path("/") or ".." in Path(raw).parts:
        raise Error("禅道后台设置的备份目录无法唯一确认。")
    return raw


def _resume_service(ctx, marker, job):
    mysql, php, nginx = job["restart"]
    docker(ctx, "start", mysql)
    _wait_mysql(ctx, mysql)
    docker(ctx, "start", php)
    if job.get("refill"):
        _extract(ctx, ["docker", "exec", "-i", php, "sh", "-ec",
                       'mkdir -p -- "$1"; exec tar -C "$1" -xpf -', "sh", job["builtin"]],
                 Path(job["output"]) / "backup.tar.gz")
        job["refill"] = False
        atomic_write(marker, json.dumps(job))
    docker(ctx, "start", nginx)
    ctx.verify((ctx.record or {}).get("stage", "site"))
    marker.unlink()


def _pending_backup(ctx, output, report, marker, dry_run):
    job = json.loads(marker.read_text())
    ids = [report["services"][n]["id"] for n in ("mysql", "php", "nginx")]
    if job["project"] != ctx.project or ids != job["restart"]:
        raise Error(f"容器已改变，请核对原备份操作：{marker}")
    if str(output) != job["output"]:
        raise Error("需先恢复上次中断前的服务。重复执行 backup --output " + shlex.quote(job["output"]))
    print(f"发现中断的备份，将按记录恢复原服务：{output}")
    if not dry_run:
        _resume_service(ctx, marker, job)
        if not (output / "manifest.json").is_file():
            raise Error("原服务已恢复，备份在完成前中断。保留此次目录，给 backup 指定一个新目录重试。")
        validate_backup(output)
    return output


def backup(ctx, output, *, dry_run=False):
    report = ctx.require_site()
    model = ctx.model()
    if set(model["services"]) not in ({"mysql", "php", "nginx"}, {"mysql", "php", "nginx", "redis"}):
        raise Error("备份只支持仓库提供的服务组合，请先核对额外写入服务。")
    output = Path(output).expanduser().resolve()
    _allowed_backup_location(output, report["evidence"])
    if ctx.engine.get("endpoint", "").startswith("unix://"):
        docker_root = Path(docker(ctx, "info", "--format", "{{.DockerRootDir}}")).resolve()
        if output == docker_root or docker_root in output.parents:
            raise Error("备份目录必须位于 Docker 引擎存储之外。")
    manual = ctx.state_dir / "backup-interrupted.json"
    if manual.exists():
        return _pending_backup(ctx, output, report, manual, dry_run)
    if output.exists() or not output.parent.is_dir():
        raise Error("请选择已存在父目录下的一个全新备份目录。")
    states = report["services"]
    for name in ("php", "mysql", "nginx"):
        if states[name]["state"] != "running":
            raise Error(f"备份需要运行中的 {name}，请先恢复站点。")
    php_id, mysql_id, nginx_id = [states[n]["id"] for n in ("php", "mysql", "nginx")]
    print(f"维护窗口内停用 {ctx.project} 的 Web、PHP、MySQL，冷备后恢复原容器。目标 {output}")
    identity = _local_db_identity(ctx, mysql_id)
    builtin = _backup_directory(ctx, mysql_id, identity["prefix"])
    if dry_run:
        return output
    for role, (svc, path) in ROLES.items():
        if role != "backup" and path not in report["evidence"]["mounts"][svc]:
            raise Error(f"无法核实 {svc} 的数据路径 {path}。")
    prior = json.loads(docker(ctx, "container", "inspect", php_id))[0]
    # Resolve the built-in backup symlink in the running image before classifying it.
    resolved = docker(ctx, "exec", php_id, "sh", "-ec", 'readlink -f "$1" || printf "%s" "$1"', "sh", builtin)
    ephemeral = any(m["Type"] == "tmpfs" and (resolved == m["Destination"] or
                    resolved.startswith(m["Destination"].rstrip("/") + "/")) for m in prior["Mounts"])
    ephemeral |= any(resolved == p or resolved.startswith(p.rstrip("/") + "/")
                     for p in (prior["HostConfig"].get("Tmpfs") or {}))
    output.mkdir(mode=0o700)
    ids = [mysql_id, php_id, nginx_id]
    job = {"project": ctx.project, "restart": ids, "output": str(output), "builtin": builtin}
    atomic_write(manual, json.dumps(job))
    try:
        docker(ctx, "stop", "--timeout", "120", nginx_id)
        if docker(ctx, "inspect", "--format", "{{.State.ExitCode}}", nginx_id) not in ("0", "143"):
            raise Error("Web 入口未正常停止，已中止冷备。")
        # Old images keep the built-in backups in tmpfs. Read them while PHP is
        # still up; the web entry has already drained. Keep the container's
        # user: hardened PHP has no capabilities and owns the private directory.
        _copy_archive(ctx, ["exec", php_id, "sh", "-ec",
                            'if test -d "$1"; then exec tar -C "$1" -cf - .; fi; '
                            'if test "$1" = /var/www/zentao/tmp/backup; then '
                            'exec tar -cf - --files-from /dev/null; fi; exit 1', "sh", builtin],
                      output / "backup.tar.gz")
        job["refill"] = ephemeral
        atomic_write(manual, json.dumps(job))
        docker(ctx, "stop", "--timeout", "120", php_id)
        docker(ctx, "stop", "--timeout", "180", mysql_id)
        if docker(ctx, "inspect", "--format", "{{.State.ExitCode}}", mysql_id) != "0":
            raise Error("MySQL 未正常结束，已中止冷备。")
        for role in ("mysql", "config", "data"):
            service, path = ROLES[role]
            _copy_archive(ctx, ["cp", "-a", states[service]["id"] + ":" + path + "/.", "-"],
                          output / (role + ".tar.gz"))
        files = [name + ".tar.gz" for name in ROLES] + ["runtime.json", "deployment.tar.gz"]
        _deployment_archive(ctx, output / "deployment.tar.gz")
        atomic_write(output / "runtime.json", json.dumps(_runtime(ctx, model, states)))
        manifest = {"format": 1, "created": datetime.now(timezone.utc).isoformat(),
                    "project": ctx.project, "images": report["evidence"]["images"], "backup_dir": builtin, "database": identity, "site": ctx.record,
                    "files": {name: _sha(output / name) for name in files}}
        atomic_write(output / "manifest.json", json.dumps(manifest, ensure_ascii=False))
    finally:
        _resume_service(ctx, manual, job)
    print(f"备份完成并恢复原容器。请保护包含站点凭据的目录 {output}")
    return output


def _wait_mysql(ctx, container):
    for _ in range(120):
        state = json.loads(docker(ctx, "inspect", "--format", "{{json .State}}", container))
        if state.get("Status") != "running":
            break
        if (state.get("Health") or {}).get("Status") == "healthy":
            return
        probe = subprocess.run(["docker", "exec", container, "mysqladmin", "ping", "--silent"],
                               env=ctx.environ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not probe.returncode:
            return
        time.sleep(1)
    raise Error("MySQL 就绪超时，请核对服务状态及冷备操作记录。")


def _new_project(ctx, project):
    if not PROJECT_RE.fullmatch(project) or project == ctx.project:
        raise Error("恢复目标必须是独立的新项目名。")
    if (ctx.state_dir.parent / project / "release.json").exists():
        raise Error("该目标项目已有部署登记。")
    if any(_ids(ctx, kind, project) for kind in ("container", "volume", "network")):
        raise Error("恢复目标项目已有 Docker 资源，请使用未占用的项目名。")


def _escape_compose(value):
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [_escape_compose(v) for v in value]
    if isinstance(value, dict):
        return {k: _escape_compose(v) for k, v in value.items()}
    return value


def _isolated_model(runtime, images, volumes, port, backup_dir="/var/www/zentao/tmp/backup"):
    result = {"services": {}, "volumes": {role: {"external": True, "name": vol}
                                         for role, vol in volumes.items()},
              "networks": {"sandbox": {"internal": True, "driver_opts": {
                  "com.docker.network.bridge.gateway_mode_ipv4": "isolated"}}, "frontend": {}}}
    for name, settings in runtime.items():
        if not isinstance(settings, dict) or set(settings) - set(RUNTIME_FIELDS):
            raise Error("恢复配置含不支持的服务字段。")
        if (set(settings.get("cap_add", [])) - {"CHOWN", "KILL", "NET_BIND_SERVICE", "SETGID", "SETUID"} or
                set(settings.get("security_opt", [])) - {"no-new-privileges:true", "no-new-privileges"}):
            raise Error("恢复配置请求了隔离实例不支持的容器特权。")
        service = dict(settings)
        service = _escape_compose(service)
        service.update(image=images[name], pull_policy="never", restart="no", networks=["sandbox"])
        result["services"][name] = service
    services = result["services"]
    services["mysql"]["volumes"] = ["mysql:/var/lib/mysql"]
    # The event scheduler must stay disabled on a restored inspection instance.
    services["mysql"]["command"] = [*services["mysql"].get("command", []), "--event-scheduler=OFF", "--skip-replica-start"]
    hardened = bool(services["php"].get("read_only"))
    if backup_dir == "/var/www/zentao/tmp/backup" and hardened:
        backup_dir = ROLES["backup"][1]
    directory = Path(backup_dir)
    allowed_roots = (Path("/var/www/zentao/tmp"), Path("/var/www/zentao/www/data"), Path("/srv"),
                     Path("/mnt"), Path("/data"), Path("/var/lib/zentao"))
    if (not directory.is_absolute() or ".." in directory.parts or ":" in backup_dir or
            not (directory == Path("/backup") or any(root in directory.parents for root in allowed_roots)) or
            directory == Path(ROLES["config"][1]) or Path(ROLES["config"][1]) in directory.parents):
        raise Error("内置备份自定义路径无法安全映射到副本，请在隔离环境单独恢复。")
    services["php"]["volumes"] = ["data:/var/www/zentao/www/data",
                                  "config:/var/lib/zentao/config" + (":ro" if hardened else ""),
                                  "backup:" + str(directory)]
    services["nginx"]["networks"].append("frontend")
    services["nginx"]["volumes"] = ["data:/var/www/zentao/www/data:ro"]
    services["nginx"]["ports"] = [f"127.0.0.1:{port}:80"]
    return result


def _put_volume(ctx, image, volume, archive):
    # Extraction happens inside a disposable, read-only, offline container with
    # a single empty destination volume. Never extract an archive on the host.
    cmd = ["docker", "run", "--rm", "-i", "--pull", "never", "--network", "none",
           "--read-only", "--user", "0:0", "--security-opt", "no-new-privileges",
           "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "FOWNER", "--cap-add", "DAC_OVERRIDE",
           "--mount", f"type=volume,src={volume},dst=/restore,volume-nocopy",
           "--entrypoint", "tar", image, "-C", "/restore", "-xpf", "-"]
    _extract(ctx, cmd, archive)


def _extract(ctx, cmd, archive):
    # Do not pass the gzip object's fileno directly: the child needs decompressed bytes.
    with gzip.open(archive, "rb") as data, subprocess.Popen(
            cmd, env=ctx.environ, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
        try:
            shutil.copyfileobj(data, proc.stdin, length=1024 * 1024)
        finally:
            proc.stdin.close()
        if proc.wait():
            raise Error("文件归档还原失败，请保留归档和恢复现场。")


def _verify_restored_database(ctx, image, volume, identity):
    database, prefix = identity["name"], identity["prefix"]
    # Start only MySQL on the copied datadir. No IP, host ports, replication,
    # server-side events, or application workers are enabled during this check.
    if not isinstance(database, str) or not database or not re.fullmatch("[A-Za-z0-9_]*", prefix):
        raise Error("无法识别副本的数据库或配置表。")
    container = docker(ctx, "create", "--network", "none", "--pull", "never",
                       "--mount", f"type=volume,src={volume},dst=/var/lib/mysql,volume-nocopy",
                       image, "--skip-networking", "--skip-grant-tables", "--skip-replica-start",
                       "--event-scheduler=OFF")
    try:
        docker(ctx, "start", container)
        _wait_mysql(ctx, container)
        # The image entrypoint can briefly answer ping using a temporary init
        # server on a freshly initialized datadir. Require actual business data.
        sql = (f"SELECT @@server_uuid, COUNT(*) FROM `{prefix}config` WHERE owner='system' "
               "AND module='common' AND `key`='version' AND value<>''")
        rows = docker(ctx, "exec", container, "mysql", "--protocol=socket", "-N", "-B",
                      "-u", "root", "--database=" + database, "--execute=" + sql)
        fields = rows.split("\t")
        if len(fields) != 2 or fields[0] != identity["uuid"] or not fields[1].isdigit() or int(fields[1]) < 1:
            raise Error("恢复副本未读到已安装禅道的数据库版本记录。")
    finally:
        docker(ctx, "stop", "--timeout", "180", container)
        docker(ctx, "rm", "-v", container)


def restore(ctx, archive, project, port, dry_run):
    source = Path(archive).expanduser().resolve()
    manifest = validate_backup(source)
    if manifest["project"] != ctx.project:
        raise Error("源项目与备份清单不同，请核对 --project。")
    _new_project(ctx, project)
    if not 1 <= port <= 65535:
        raise Error("恢复验收端口超出范围。")
    runtime = json.loads((source / "runtime.json").read_text())
    images = manifest["images"]
    if set(runtime) != set(images):
        raise Error("备份中的服务与所需镜像清单不一致。")
    for name, image in images.items():
        if docker(ctx, "image", "inspect", "--format", "{{.Id}}", image) != image:
            raise Error(f"缺少备份指定的 {name} 镜像。")
    spec = _isolated_model(runtime, images, dict.fromkeys(ROLES, ""), port, manifest["backup_dir"])
    target = ctx.state_dir / "restore" / project
    if target.exists():
        raise Error(f"恢复操作已有记录，请核对 {target} 或另选新项目。")
    nonce = uuid.uuid4().hex
    volumes = {role: f"{project}_restore_{role}_{nonce[:12]}" for role in ROLES}
    print(f"从 {source} 恢复到独立项目 {project} 的四个新卷，应用保持停止。")
    if dry_run:
        return
    target.mkdir(mode=0o700, parents=True)
    atomic_write(target / "restore.json", json.dumps({"target": project, "volumes": volumes,
                                                      "source": str(source), "complete": False}))
    for role, name in volumes.items():
        created = docker(ctx, "volume", "create", "--label", f"{PROJECT_LABEL}={project}",
                         "--label", f"{RESTORE_LABEL}={nonce}", name)
        details = json.loads(docker(ctx, "volume", "inspect", created))[0]
        if created != name or details.get("Options") or details.get("Labels", {}).get(RESTORE_LABEL) != nonce:
            raise Error(f"未能为 {role} 建立该副本的独占卷。")
        _put_volume(ctx, images["nginx"], name, source / (role + ".tar.gz"))
    identity = manifest["database"]
    _verify_restored_database(ctx, images["mysql"], volumes["mysql"], identity)
    spec["volumes"] = {role: {"external": True, "name": name} for role, name in volumes.items()}
    compose = target / "compose.json"
    atomic_write(compose, json.dumps(spec, ensure_ascii=False, indent=2))
    atomic_write(target / "restore.json", json.dumps({"target": project, "volumes": volumes,
                                                      "source": str(source), "complete": True}))
    print(f"已从恢复副本读取禅道版本记录。私有 Compose 配置：{compose}")
    print(f"确认数据库和缓存目标均属于副本、后台作业和通知已受限后，可用 "
          f"docker compose -p {project} -f {shlex.quote(str(compose))} up -d --wait。")
    print(f"浏览器访问 http://127.0.0.1:{port}，登录并检查已知项目和历史附件。")


def uninstall(ctx, dry_run):
    report = ctx.require_site()
    if (ctx.state_dir / "backup-interrupted.json").exists():
        raise Error("检测到未完成的备份，请先用相同的 backup --output 恢复原服务。")
    states = report["services"]
    if any(states[n]["state"] != "running" for n in ("php", "mysql")):
        raise Error("请先 recover 启动 PHP 和 MySQL，以便卸载前识别内置备份位置。")
    php = states["php"]["id"]
    if (ctx.record or {}).get("stage") == "install":
        backup_dir = "/var/www/zentao/tmp/backup"
    else:
        mysql = states["mysql"]["id"]
        identity = _local_db_identity(ctx, mysql)
        backup_dir = _backup_directory(ctx, mysql, identity["prefix"])
    resolved = docker(ctx, "exec", php, "sh", "-ec", 'readlink -f "$1" || printf "%s" "$1"', "sh", backup_dir)
    mounts = report["evidence"]["mounts"]["php"]
    persistent = any(m["type"] in ("volume", "bind") and (resolved == p or resolved.startswith(p + "/"))
                     for p, m in mounts.items())
    print(f"卸载 {ctx.project} 的容器和网络。保留数据库、附件、配置、备份卷和登记。")
    if not persistent:
        outdir = ctx.state_dir / "uninstall-backups"
        print(f"容器临时层中的内置备份将在卸载前导出到 {outdir}")
    if not dry_run:
        if not persistent:
            outdir.mkdir(mode=0o700, exist_ok=True)
            path = outdir / (uuid.uuid4().hex + ".tar.gz")
            nginx = states["nginx"]
            if nginx["state"] == "running":
                docker(ctx, "stop", "--timeout", "120", nginx["id"])
            try:
                _copy_archive(ctx, ["exec", php, "sh", "-ec",
                                    'if test -d "$1"; then exec tar -C "$1" -cf - .; fi; '
                                    'exec tar -cf - --files-from /dev/null', "sh", backup_dir], path)
            except BaseException:
                if nginx["state"] == "running":
                    docker(ctx, "start", nginx["id"])
                raise
            print(f"已导出 {path}")
        ctx.dc("down")
    return report


def purge(ctx, args):
    if not ctx.record:
        raise Error("需要原实例的登记记录。")
    if _ids(ctx, "container", ctx.project):
        raise Error("该项目仍有容器，请先卸载并核对要保留的备份。")
    evidence = ctx.record.get("evidence") or {}
    if not evidence.get("mounts"):
        raise Error("登记缺少原卷证据。")
    protected = {m.get("name") for svc in evidence["mounts"].values()
                 for path, m in svc.items() if path == ROLES["backup"][1]}
    model = ctx.model()
    if list(map(str, ctx.files())) != ctx.record["files"] or ctx.signature(ctx.files()) != ctx.record["signature"]:
        raise Error("当前 Compose 配置与原站登记不符。")
    external = {v["name"] for v in model.get("volumes", {}).values() if v.get("external")}
    if getattr(args, "confirm_dir", None):
        if args.confirm_volume:
            raise Error("目录与命名卷请分别预演并逐项确认。")
        from .purge_bind import purge_bind
        return purge_bind(ctx, args)
    if getattr(args, "from_backup", None):
        manifest = validate_backup(args.from_backup)
        if manifest["project"] != ctx.project or manifest["site"].get("engine") != ctx.engine:
            raise Error("备份项目或来源引擎不同，请核对要保留的恢复点。")
        print(f"已校验备份清单及摘要 {Path(args.from_backup).resolve()}")
    else:
        print("本次预检未提供备份目录，可用 --from-backup 指定要保留的恢复点。")
    present = set(docker(ctx, "volume", "ls", "-q").splitlines())
    absent = set()
    candidates = {}
    for role in ("mysql", "config", "data"):
        service, path = ROLES[role]
        m = evidence["mounts"].get(service, {}).get(path, {})
        if m.get("type") != "volume" or m.get("driver") != "local" or m.get("source") or m.get("device"):
            print(f"保留宿主机或外部存储 {m.get('device') or m.get('source') or path}。专属本机目录可用 --confirm-dir 与 --from-backup 另行预检。")
            continue
        name = m.get("name")
        if not name or name in protected or name in external:
            continue
        if name not in present:
            absent.add(name)
            print(f"卷已不存在 {name}")
            continue
        details = json.loads(docker(ctx, "volume", "inspect", name))[0]
        if (not m.get("created") or details.get("CreatedAt") != m["created"] or
                (details.get("Labels") or {}).get(PROJECT_LABEL) != ctx.project or
                details.get("Options") or details.get("Driver") != "local"):
            raise Error(f"卷 {name} 的创建记录、项目归属或驱动不符。")
        if docker(ctx, "ps", "-aq", "--filter", "volume=" + name):
            raise Error(f"卷 {name} 仍被容器引用。")
        candidates[name] = role
    print("可逐项销毁的专属命名卷：" + ("、".join(candidates) or "无"))
    selected = set(args.confirm_volume or [])
    if args.dry_run:
        return
    if args.confirm_project != ctx.project or not selected or not selected <= candidates.keys() | absent:
        raise Error("需要 --confirm-project 原项目名，并用 --confirm-volume 逐个指定清单内的卷。")
    for name in sorted(selected - absent):
        docker(ctx, "volume", "rm", name)
        print(f"已删除 {name}")


def handle(ctx, args):
    if args.command == "backup":
        return backup(ctx, args.output, dry_run=args.dry_run)
    if args.command == "restore":
        lock = contextlib.nullcontext() if args.dry_run else project_lock(ctx.engine, args.to_project)
        with lock:
            return restore(ctx, args.from_backup, args.to_project, getattr(args, "port", None) or 18080, args.dry_run)
    if args.command == "uninstall":
        return uninstall(ctx, args.dry_run)
    if args.command == "purge-data":
        return purge(ctx, args)
    raise Error("未知数据操作。")
