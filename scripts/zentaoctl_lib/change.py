"""Plan configuration changes, publish local builds and install a second blank site."""
import argparse
from contextlib import contextmanager
import copy
import ipaddress
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile

from . import site
from .env_edit import render_env
from .witness import verify_identity
from .common import Context, Error, atomic_write, require_clean, valid_project


def _port(value):
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口范围为 1 到 65535。")
    return port


def add_arguments(command, parser):
    if command == "configure":
        parser.add_argument("--port", type=_port)
        parser.add_argument("--bind", metavar="IP", help="Nginx 监听 IP")
        parser.add_argument("--cache", choices=["none", "redis", "apcu"])
        parser.add_argument("--backup-dir", help="更换缓存驱动时保存冷备份的新目录")
    elif command == "update":
        parser.add_argument("--app-ref", help="校验当前 zentaopms 检出版本")
        parser.add_argument("--deployment-ref", help="校验当前部署仓库检出版本")
        parser.add_argument("--backup-dir", required=True, help="切换前保存冷备份的新目录")
    elif command == "reinstall":
        parser.add_argument("--target-project", required=True, help="独立新站的项目名")
        parser.add_argument("--target-env-file", required=True, help="新站专用的环境文件")
        parser.add_argument("--port", type=_port, required=True, help="新站回环端口")
        parser.add_argument("--data-dir", help="新站独占的空目录，默认使用独立命名卷")
        parser.add_argument("--cache", choices=["none", "redis", "apcu"], default="none")
        parser.add_argument("--backup-dir", required=True, help="旧站备份的新目录")


def _git(root, *args):
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if proc.returncode:
        raise Error(f"无法核实 {root.name} 的 Git 版本。")
    return proc.stdout.strip()


def _checkout(root, ref):
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if ref and _git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}") != head:
        raise Error(f"{root.name} 的检出版本与所选引用不符。请先在部署主机准备目标提交。")
    if _git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise Error(f"{root.name} 包含尚未提交的文件，无法把构建结果归属到提交 {head[:12]}。请保留工作成果后使用干净检出。")
    return head


class _PreviousRelease(Context):
    """Evaluate old file bodies while inspecting the original Compose file labels."""

    def __init__(self, current, overrides):
        self.__dict__.update(vars(current))
        self.overrides = overrides

    def compose(self, files=None):
        original = self.files() if files is None else files
        return super().compose([self.overrides.get(f, f) for f in original])


@contextmanager
def _baseline(ctx, command):
    files = ctx.files()
    if (command != "update" or not ctx.record or
            ctx.signature(files) == ctx.record["signature"]):
        yield ctx
        return
    revision = ctx.record.get("compose_revision") or ""
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision) or list(map(str, files)) != ctx.record["files"]:
        raise Error("无法定位原 Compose 文件版本或文件集不同，请恢复原文件与顺序后更新。")
    with tempfile.TemporaryDirectory(prefix="zentaoctl-prior-compose-") as directory:
        prior_paths = {}
        for index, path in enumerate(files):
            if ctx.root not in path.parents:
                continue
            spec = revision + ":" + path.relative_to(ctx.root).as_posix()
            proc = subprocess.run(["git", "-C", str(ctx.root), "show", spec], capture_output=True)
            if proc.returncode:
                continue  # User overlays and generated image references remain in place.
            if proc.stdout != path.read_bytes():
                previous = Path(directory) / f"{index}.yaml"
                previous.write_bytes(proc.stdout)
                prior_paths[path] = previous
        baseline = _PreviousRelease(ctx, prior_paths)
        if baseline.signature(files) != ctx.record["signature"]:
            raise Error("旧版 Compose 配合当前环境文件与自定义覆盖无法重现已验收配置。请核实原数据、凭据及覆盖文件。")
        yield baseline


def _cache_files(ctx, files, cache):
    standard = {ctx.root / "compose.redis.yaml", ctx.root / "compose.apcu.yaml"}
    indices = [i for i, path in enumerate(files) if path in standard]
    if len(indices) > 1:
        raise Error("当前文件集中包含多个缓存覆盖文件。")
    if indices:
        position = indices[0]
    else:
        # Standard overlays precede site-specific overrides.
        position = 1
        if ctx.root / "compose.persistence.yaml" in files:
            position = files.index(ctx.root / "compose.persistence.yaml") + 1
    keep = [path for path in files if path not in standard]
    if cache != "none":
        keep.insert(position, ctx.root / f"compose.{cache}.yaml")
    return keep


def _storage(model):
    """Compare physical definitions, including bind devices and custom mounts."""
    defs = model.get("volumes", {})
    mounts = {}
    for name, svc in model["services"].items():
        for mount in svc.get("volumes", []):
            source = mount.get("source")
            mounts[name, mount["target"]] = (mount, defs.get(source))
    return mounts


def _cache(model):
    return model["services"]["php"].get("build", {}).get("args", {}).get(
        "ZENTAO_CACHE_EXTENSION", "none")


def _check_ports(current, target):
    old = {str(p.get("published")) for p in current["services"]["nginx"].get("ports", [])}
    for item in target["services"]["nginx"].get("ports", []):
        port = str(item.get("published", ""))
        if not port or port in old or item.get("protocol", "tcp") != "tcp":
            continue
        host = item.get("host_ip") or "0.0.0.0"
        try:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            with socket.socket(family) as probe:
                probe.bind((host, int(port)))
        except (OSError, ValueError):
            raise Error(f"无法绑定目标地址 {host} 的 TCP 端口 {port}，原站继续运行。") from None


def _publish_args(ctx, args, model):
    env = {}
    if args.command != "configure":
        return env, _cache(model)
    if args.port is None and args.bind is None and args.cache is None:
        raise Error("configure 需要 --port、--bind 或 --cache。")
    if args.port is not None:
        env["ZENTAO_PORT"] = str(args.port)
    if args.bind is not None:
        try:
            address = ipaddress.ip_address(args.bind)
        except ValueError:
            raise Error("--bind 需要 IPv4 或 IPv6 地址。") from None
        # The current compose.yaml interpolates this into a short port mapping.
        env["ZENTAO_BIND"] = f"[{address}]" if address.version == 6 else str(address)
    return env, args.cache or _cache(model)


def _check_build(ctx, model, args, baseline):
    from .install import installed_version

    app = ctx.root.parent / "zentaopms"
    for name in ("php", "nginx"):
        build = model["services"][name].get("build") or {}
        if build.get("target") not in {name, f"{name}-hardened"}:
            raise Error(f"{name} 缺少仓库已支持的构建目标，请核对自定义覆盖。")
        if not build.get("context") or Path(build["context"]).resolve() != app:
            raise Error(f"{name} 使用自定义应用构建目录，请单独核实版本来源。")
    app_commit = _checkout(app, getattr(args, "app_ref", None))
    deploy_commit = _checkout(ctx.root, getattr(args, "deployment_ref", None))
    if args.command == "configure":
        known = ctx.record.get("commits", {})
        if known.get(app.name) != app_commit or known.get(ctx.root.name) != deploy_commit:
            raise Error("工作树版本与登记版本不同，请先使用 update 验证和发布新镜像。")
    source = _git(app, "show", "HEAD:config/config.php")
    version = re.search(r"^\$config->version\s*=\s*'([\w.\-]+)'\s*;", source, re.M)
    current = installed_version(baseline)
    if not version or not current or version[1] != current:
        raise Error("应用源码版本与已安装数据库版本不同或无法核实。请先在隔离副本备份恢复演练，并在受限维护入口完成禅道数据库升级；在线切换已暂停。")
    print(f"目标应用 {app_commit[:12]}，部署 {deploy_commit[:12]}，数据库版本 {current}。")
    return app_commit, deploy_commit


def _images(ctx, model):
    result = {}
    for name, service in model["services"].items():
        ref = service.get("image") or f"{ctx.project}-{name}"
        result[name] = ctx.docker("image", "inspect", "--format", "{{.Id}}", "--", ref).strip()
    return result


def _switch(ctx, operation):
    from .env_edit import write_env

    files = list(map(Path, operation["files"]))
    # Only the preflighted candidate or the exact original may be present on retry.
    current = ctx.signature(ctx.files())
    options = dict(operation["settings"])
    if current != operation["candidate_signature"]:
        if current != operation["original_signature"]:
            raise Error("切换中断后生效配置又被修改，请恢复原环境文件或已准备的目标配置。")
        secret_path = ctx.state_dir / "change-redis.secret"
        if operation["generated_redis"]:
            if not secret_path.is_file():
                raise Error("变更所需 Redis 密钥缺失，请恢复实例私有维护目录。")
            options["ZT_REDIS_PASSWORD"] = secret_path.read_text().strip()
        from .install import preview_env
        with preview_env(ctx, render_env(ctx.env_file, options)) as preview:
            if preview.signature(files) != operation["candidate_signature"]:
                raise Error("候选镜像或 Compose 文件在预检后已变更。")
        write_env(ctx.env_file, options)

    if ctx.files() != files or ctx.signature(files) != operation["candidate_signature"]:
        raise Error("生效配置与候选不符，已暂停切换。")
    report = ctx.snapshot(files=files)
    # During a partial switch the old containers and the new configuration differ.
    # Compare data destinations with the accepted release before any up/create.
    for name, previous in operation["mounts"].items():
        if name == "redis" and operation["retire_redis"] and not previous:
            continue
        if previous != report["evidence"]["mounts"].get(name):
            raise Error(f"{name} 的原登记数据卷或设备发生变化，已暂停切换。")
    if _images(ctx, ctx.model(files)) != operation["images"]:
        raise Error("候选镜像标签已指向其他镜像，已暂停切换。")
    extra = set(report["services"]) - set(operation["images"])
    if extra - ({"redis"} if operation["retire_redis"] else set()):
        raise Error("项目出现未纳入变更计划的容器。")
    for name, service in report["services"].items():
        allowed = {operation["images"].get(name), operation["prior_images"].get(name)}
        if service["image_id"] not in allowed or Path(service["directory"]).resolve() != ctx.root:
            raise Error(f"{name} 的运行镜像或来源目录不属于已核实的切换计划。")
    if operation["retire_redis"] and "redis" in report["services"]:
        if report["services"]["redis"]["id"] != operation["retire_redis"]:
            raise Error("待退役的 Redis 容器身份已变化，已暂停。")

    for name, mounts in operation["mounts"].items():
        if name not in report["services"]:
            continue
        live = report["services"][name]["mounts"]
        for destination, expected in mounts.items():
            actual = live.get(destination, {})
            key = "name" if expected["type"] == "volume" else "source"
            if any(actual.get(k) != expected[k] for k in ("type", key, "read_only")):
                raise Error(f"{name} 的实际挂载 {destination} 已改变，请核对原数据。")
    verify_identity(ctx, report)
    force = operation["rebuild"]
    refs_ok = all(report["services"].get(k, {}).get("image_id") == v for k, v in operation["images"].items())
    rolled = operation.get("started", False)
    if not rolled or not refs_ok or report["missing"]:
        if force:
            ctx.dc("up", "-d", "--force-recreate", "--no-build", "--pull", "never", "--wait",
                   "--wait-timeout", "180", files=files)
        else:
            ctx.dc("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait",
                   "--wait-timeout", "180", "nginx", files=files)
        operation["started"] = True
        ctx.set_intent(operation)
    ctx.verify(stage=operation["stage"], files=files)
    if operation["retire_redis"] and "redis" in report["services"]:
        # Only the captured official cache container is removed. Never remove volumes.
        cid = operation["retire_redis"]
        ctx.docker("container", "stop", cid)
        ctx.docker("container", "rm", cid)
    final = ctx.snapshot(files=files)
    require_clean(final)
    ctx.save(final, stage=operation["stage"], files=files)
    ctx.set_intent(None)
    if operation["generated_redis"]:
        (ctx.state_dir / "change-redis.secret").unlink(missing_ok=True)
    print("已验收并登记当前配置。请登录复查原项目、历史附件及后台缓存设置。")


def _request(args):
    return {name: getattr(args, name, None) for name in
            ("port", "bind", "cache", "app_ref", "deployment_ref", "backup_dir")}


def _complete_backup(ctx, baseline, operation, dry_run):
    from . import dataops

    output = Path(operation["backup"])
    baseline.require_site()
    marker = ctx.state_dir / "backup-interrupted.json"
    if not output.exists() or marker.exists():
        dataops.backup(baseline, output, dry_run=dry_run)
        if dry_run:
            return
    try:
        stored = dataops.validate_backup(output)
    except Error as exc:
        raise Error(f"上次备份不完整或摘要校验失败。请保留并移走 {output}，再用相同参数重试。") from exc
    accepted = stored.get("site", {})
    if (stored.get("project") != ctx.project or accepted.get("engine", {}).get("id") != ctx.engine["id"]
            or accepted.get("signature") != baseline.record["signature"]
            or accepted.get("witnesses") != baseline.record["witnesses"]):
        raise Error("备份的项目、数据身份或配置与原实例不符，请核对备份位置。")
    baseline.require_site()


def _publish(ctx, args):
    if ctx.intent:
        if ctx.intent.get("command") != args.command or "candidate_signature" not in ctx.intent:
            raise Error("存在其他未完成操作，请先查看 status。")
        operation = dict(ctx.intent)
        if operation["request"] != _request(args):
            raise Error("已有待完成变更，请使用相同的端口、缓存、版本和备份目录重试。")
        print("继续上次已准备的候选，使用已登记的参数、镜像和数据卷。")
        if operation.get("backup_pending"):
            with _baseline(ctx, args.command) as baseline:
                _complete_backup(ctx, baseline, operation, args.dry_run)
            if not args.dry_run:
                operation["backup_pending"] = False
                ctx.set_intent(operation)
        if not args.dry_run:
            _switch(ctx, operation)
        return

    with _baseline(ctx, args.command) as baseline:
        _prepare_change(ctx, baseline, args)


def _prepare_change(ctx, baseline, args):
    from . import dataops
    from .install import build_candidates, without_candidates

    if not ctx.engine.get("endpoint", "").startswith("unix://"):
        raise Error("更新后需要在 Docker 主机本地验收站点，请从该主机执行。")
    original = baseline.require_site()
    model = baseline.model()
    files = ctx.files()
    original_signature = ctx.signature(files)
    updates, cache = _publish_args(ctx, args, model)
    old_cache = _cache(model)
    if old_cache not in {"redis", "apcu", "none"}:
        raise Error("当前站点使用自定义缓存扩展，请先核实构建目标。")
    cache_changed = cache != old_cache
    rebuild = args.command == "update" or cache_changed
    backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else None
    if rebuild and not backup_dir:
        raise Error("重建前需要 --backup-dir，备份包括旧 PHP 容器中的本地备份。")
    if rebuild:
        commits = _check_build(ctx, model, args, baseline)
    candidate = copy.copy(ctx)
    generated_redis = False
    if cache_changed:
        files = _cache_files(ctx, without_candidates(ctx, files), cache)
        if cache == "redis":
            password = ctx.settings().get("ZT_REDIS_PASSWORD", "")
            if not password:
                updates["ZT_REDIS_PASSWORD"] = secrets.token_hex(24)
                generated_redis = True
            elif not re.fullmatch(r"[0-9a-fA-F]{32,}", password):
                raise Error("现有 ZT_REDIS_PASSWORD 不满足 Redis 长度与格式要求。")
    candidate.environ = dict(ctx.environ, **updates)
    target = candidate.model(files)
    if _cache(target) != cache or ("redis" in target["services"]) != (cache == "redis"):
        raise Error("自定义覆盖与所选缓存发生冲突。")
    if cache_changed and old_cache == "redis":
        service = model["services"].get("redis", {})
        command = service.get("command", [])
        if ("save \"\"" not in " ".join(command) or "appendonly no" not in " ".join(command)
                or service.get("volumes") or not str(service.get("image", "")).startswith("redis:")):
            raise Error("现有 Redis 使用自定义镜像或持久化策略，请单独迁移该服务。")
    if _storage(target) != _storage(model):
        raise Error("候选配置改变了卷、目录或挂载定义，请使用单独的数据迁移。")
    _check_ports(model, target)
    render_env(ctx.env_file, dict(updates, COMPOSE_FILE=ctx.settings().get("COMPOSE_FILE", "")))
    if args.command == "update":
        if set(model["services"]) != set(target["services"]):
            raise Error("部署版本改变了服务集合，请单独规划组件变更。")
        for name in ("mysql", "redis"):
            if name not in model["services"]:
                continue
            old = model["services"][name].get("image", "")
            new = target["services"][name].get("image", "")
            if old != new and ("@sha256:" not in new or old.split("@")[0] != new.split("@")[0]):
                raise Error(f"{name} 更换了镜像系列或使用未固定版本；请先演练服务数据迁移。")
    if args.command == "configure":
        ports = target["services"]["nginx"].get("ports", [])
        http = [p for p in ports if p["target"] == 80 and p.get("published")]
        if len(http) != 1 or (args.port is not None and http[0]["published"] != str(args.port)) or (
                args.bind and http[0].get("host_ip") != str(ipaddress.ip_address(args.bind))):
            raise Error("站点存在自定义端口映射，无法唯一应用所选监听设置。")
    if not rebuild:
        if target == model:
            print("生效配置已匹配，无需重建。")
            return
        port_change = copy.deepcopy(model)
        port_change["services"]["nginx"]["ports"] = target["services"]["nginx"]["ports"]
        if target != port_change:
            raise Error("监听变量还改变了其他运行配置，请检查自定义覆盖，已保留原站。")
    if rebuild:
        dataops.backup(baseline, backup_dir, dry_run=True)
    print(f"站点 {ctx.project}，缓存 {old_cache} → {cache}，入口 {baseline.url()} → {candidate.url(files)}。")
    print("将构建 PHP 与 Nginx，备份后在维护窗口替换项目容器。" if rebuild else "将替换 Nginx 以应用监听设置。")
    if cache_changed:
        print("切换缓存前请核实禅道后台的缓存驱动配置。")
    if args.dry_run:
        return
    if rebuild:
        mode = "hardened" if model["services"]["php"]["build"]["target"] == "php-hardened" else "normal"
        files = build_candidates(candidate, files, mode=mode, cache=cache)
        if _check_build(ctx, target, args, baseline) != commits:
            raise Error("源码在候选构建期间发生变动，请重新准备提交。")
        # Remote service images are pinned by digest in this repository.
        for service in ("mysql", "redis"):
            wanted = target["services"].get(service, {}).get("image")
            existing = model["services"].get(service, {}).get("image")
            if wanted and wanted != existing:
                candidate.dc("pull", service, files=files)
        baseline.require_site()
    separator = ctx.settings().get("COMPOSE_PATH_SEPARATOR") or os.pathsep
    if any(separator in str(path) for path in files):
        raise Error("Compose 路径与配置的路径分隔符冲突。")
    updates["COMPOSE_FILE"] = separator.join(str(p) for p in files)
    from .install import preview_env
    with preview_env(ctx, render_env(ctx.env_file, updates)) as rendered:
        signature = rendered.signature(files)
        if _storage(rendered.model(files)) != _storage(model):
            raise Error("渲染环境文件后数据卷定义发生变化，已暂停切换。")
    # Persist credentials only in a private temporary secret, never in operation.json.
    if generated_redis:
        atomic_write(ctx.state_dir / "change-redis.secret", updates.pop("ZT_REDIS_PASSWORD"))
    operation = {
        "command": args.command, "files": list(map(str, files)), "settings": updates,
        "request": _request(args), "backup": str(backup_dir) if backup_dir else None, "backup_pending": rebuild,
        "generated_redis": generated_redis, "original_signature": original_signature,
        "candidate_signature": signature, "mounts": original["evidence"]["mounts"],
        "images": _images(candidate, candidate.model(files)), "prior_images": original["evidence"]["images"],
        "rebuild": rebuild,
        "retire_redis": original["services"]["redis"]["id"] if cache_changed and old_cache == "redis" else None,
    }
    if ctx.record.get("stage") in {"install", "installer"}:
        from .install import installed_version
        operation["stage"] = "site" if installed_version(baseline) else "install"
    else:
        operation["stage"] = "site"
    ctx.set_intent(operation)
    if rebuild:
        _complete_backup(ctx, baseline, operation, dry_run=False)
        operation["backup_pending"] = False
        ctx.set_intent(operation)
    _switch(ctx, operation)


def _reinstall(ctx, args):
    from . import dataops

    destination = args.target_project
    target_env = Path(args.target_env_file).resolve()
    backup_dir = Path(args.backup_dir).resolve()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else None
    if data_dir and (data_dir == backup_dir or data_dir in backup_dir.parents or
                     backup_dir in data_dir.parents or data_dir in target_env.parents):
        raise Error("新站存储、环境文件和旧站备份需要独立路径。")
    if target_env == backup_dir or backup_dir in target_env.parents:
        raise Error("新站环境文件不能位于旧站备份目录。")
    if not valid_project(destination) or destination == ctx.project or target_env == ctx.env_file:
        raise Error("重新安装需要不同的项目名和专用环境文件。")
    options = [str(ctx.root / "scripts/zentaoctl"), "install", "--project", destination,
               "--env-file", str(target_env), "--port", str(args.port), "--cache", args.cache,
               "--storage", "bind" if data_dir else "volume"]
    if data_dir:
        options += ["--data-dir", str(data_dir)]
    plan = {"command": "reinstall", "options": options, "backup": str(backup_dir)}
    pending = ctx.intent
    if pending and any(pending.get(k) != v for k, v in plan.items()):
        raise Error("请使用相同的项目、备份目录、新站路径与端口重试上次重新安装。")
    ctx.require_site()
    if not pending and (target_env.exists() or destination in site.discover() or
                        (ctx.state_dir.parent / destination).exists()):
        raise Error("目标实例、登记目录或环境文件已存在，请选择专属的新项目。")
    old_ports = ctx.model()["services"]["nginx"].get("ports", [])
    if any(str(p.get("published")) == str(args.port) for p in old_ports):
        raise Error("新站需使用不同于旧站的端口。")

    def invoke_install(preview):
        result = subprocess.run(options + (["--dry-run"] if preview else []), cwd=ctx.root, env=ctx.environ)
        if result.returncode:
            raise Error("独立安装尚未完成，已保留旧站和已有备份。")

    print(f"旧站 {ctx.project}，将备份至 {backup_dir}；空库新站 {destination} 使用回环端口 {args.port}。")
    if not pending:
        invoke_install(True)
        dataops.backup(ctx, backup_dir, dry_run=True)
        if args.dry_run:
            return
        ctx.set_intent(plan)
    _complete_backup(ctx, ctx, plan, args.dry_run)
    if args.dry_run:
        return
    invoke_install(False)
    ctx.set_intent(None)
    print(f"旧站继续使用原入口。请在新站完成网页向导，按需 harden --project {destination} 并验收，然后切换代理或监听端口。")


def handle(ctx, args):
    if args.command == "reinstall":
        _reinstall(ctx, args)
    else:
        _publish(ctx, args)
