"""Command line and operations on an existing Compose project."""
import argparse
import contextlib
import os
from pathlib import Path
import shlex
import sys

from . import site
from .common import Context, Error, project_lock, read_json, require_clean, valid_project
from .witness import verify_identity

ROOT = Path(__file__).resolve().parents[2]
ACTIONS = {
    "status": "查看运行现场与数据挂载", "adopt": "核实现有站点并登记",
    "recover": "用原数据和已验证镜像恢复", "stop": "有序停止现有容器",
    "restart": "重启现有容器并验收", "install": "在独立空存储安装新站",
    "harden": "将已安装站点切换至加固镜像", "configure": "变更已登记站点配置",
    "update": "用已准备的源码版本构建并更新", "reinstall": "备份并安装并行的新实例",
    "backup": "创建实例离线备份", "restore": "将备份还原到独立卷",
    "uninstall": "移除实例容器和网络，保留数据", "purge-data": "按确认清单清理专属卷或宿主机数据子目录",
}


def parser():
    p = argparse.ArgumentParser(prog="zentaoctl", description="禅道 Compose 实例维护")
    subs = p.add_subparsers(dest="command", required=True)
    for name, description in ACTIONS.items():
        cmd = subs.add_parser(name, help=description, description=description)
        cmd.add_argument("--project", help="原 Compose 项目名")
        cmd.add_argument("--env-file", help="此实例的环境文件")
        if name != "status":
            cmd.add_argument("--dry-run", action="store_true", help="显示预检结果和计划")
        if name == "status":
            cmd.add_argument("--verify", action="store_true", help="检查 HTTP 入口与 PHP 依赖")
        if name == "install":
            cmd.add_argument("--storage", choices=["volume", "bind"])
            cmd.add_argument("--data-dir")
            cmd.add_argument("--cache", choices=["none", "apcu", "redis"])
            cmd.add_argument("--port", type=int)
        if name in ("update", "configure", "reinstall"):
            from .change import add_arguments
            add_arguments(name, cmd)
        if name == "backup":
            cmd.add_argument("--output", required=True)
        if name == "restore":
            cmd.add_argument("--from-backup", required=True)
            cmd.add_argument("--to-project", required=True)
            cmd.add_argument("--port", type=int, default=18080)
        if name == "purge-data":
            cmd.add_argument("--confirm-project")
            cmd.add_argument("--confirm-volume", action="append", default=[])
            cmd.add_argument("--confirm-dir", action="append", default=[])
            cmd.add_argument("--from-backup", help="已验证的外部备份目录")
    return p


def report_status(ctx, report):
    print(f"项目 {ctx.project}    引擎 {ctx.engine['context']}    环境文件 {ctx.env_file}")
    print("配置文件 " + " → ".join(str(p) for p in ctx.files()))
    if ctx.record:
        print("上次验收阶段 " + ctx.record["stage"])
    else:
        print("此实例尚未登记。")
    if ctx.intent:
        print("尚有未完成操作 " + intent_command(ctx.intent))
    for name, svc in sorted(report["services"].items()):
        tag = report["evidence"]["targets"].get(name, "")
        print(f"  {name}: {svc['state']} {svc['health']} {tag} {svc['image_id'][:19]}")
    for svc, mounts in report["evidence"]["mounts"].items():
        for path, detail in mounts.items():
            print(f"  {svc} {path} ← {detail['source'] or detail['name']}")
    if report["missing"]:
        print("缺少容器 " + ", ".join(report["missing"]))
    for volume in report.get("extra_volumes", []):
        print(f"  保留未使用的项目卷 {volume}")
    for issue in report["blockers"] + report["unverified"]:
        print("  • " + issue)


def intent_command(intent):
    return str(intent.get("command") or intent.get("operation") or intent.get("action") or "")


def trusted_images(report):
    if report.get("unverified"):
        raise Error("\n".join(report["unverified"]) + "\n请核实镜像来源，再登记或恢复。")


def status(ctx, args):
    try:
        report = ctx.snapshot((ctx.record or {}).get("evidence"))
    except Error:
        rows = ctx.docker("ps", "-a", "--filter", "label=com.docker.compose.project=" + ctx.project,
                          "--format", '{{.Names}} {{.Status}} {{.Label "com.docker.compose.project.config_files"}}')
        print(f"项目 {ctx.project} 的容器、状态及历史文件集：")
        print(rows.strip() or "未找到容器。")
        raise
    report_status(ctx, report)
    if ctx.record:
        if [str(p) for p in ctx.files()] != ctx.record["files"] or ctx.signature(ctx.files()) != ctx.record["signature"]:
            report["blockers"].append("当前配置与上次登记不一致。")
            print(report["blockers"][-1])
    if args.verify:
        require_clean(report)
        trusted_images(report)
        if ctx.record:
            verify_identity(ctx, report)
        ctx.verify(current_stage(ctx))
    return 1 if report["blockers"] or report["missing"] else 0


def current_stage(ctx):
    if (ctx.record or {}).get("stage") in ("install", "installer"):
        # A user may have finished the web wizard since the last invocation.
        from .install import installed_version
        return "site" if installed_version(ctx) else "install"
    return "site"


def existing(ctx, args):
    command = args.command
    if command == "adopt" and not ctx.record:
        report = ctx.snapshot()
        report_status(ctx, report)
        require_clean(report)
        trusted_images(report)
        from .install import installed_version
        from .witness import capture
        stage = "site" if installed_version(ctx) else "install"
        ctx.verify(stage)
        if args.dry_run:
            capture(ctx, report, stage)
            print("计划登记已核实的项目、卷与镜像。容器保持原状。")
            return
        ctx.save(report, stage)
        print(f"已登记 {ctx.project}，后续可用 recover --project {ctx.project}。")
        return

    report = ctx.registered()
    require_clean(report, allow_missing=command == "recover")
    trusted_images(report)
    running = all(s["state"] == "running" and s["health"] in ("", "healthy")
                  for s in report["services"].values()) and not report["missing"]
    if command == "adopt":
        verify_identity(ctx, report)
        print(f"{ctx.project} 已登记并匹配。")
        return
    if args.dry_run:
        verify_identity(ctx, report)
        print(f"项目 {ctx.project}，服务 " + ", ".join(sorted(report["evidence"]["images"])))
        if command == "recover" and running:
            print("现场一致，恢复只执行验收，预计重建 0 个容器。")
        elif command == "recover":
            print("计划只读核验原数据，启动原容器或补建缺失容器。数据身份不符时会暂停。")
        else:
            print(f"将对已登记容器执行 {command}。")
        return
    verify_identity(ctx, report)
    if command == "stop":
        ctx.dc("stop")
        print(f"已停止 {ctx.project}，原容器及其数据保留。")
        return
    if command == "restart":
        ctx.dc("restart")
    if (command == "recover" and not running) or command == "restart":
        ctx.dc("up", "-d", "--no-recreate", "--no-build", "--pull", "never", "--wait",
               "--wait-timeout", "180")
        report = ctx.require_site()
        verify_identity(ctx, report)
    ctx.verify(current_stage(ctx))
    print(f"{ctx.project} 验收通过。")


def select_project(args, engine):
    if args.project:
        if not valid_project(args.project):
            raise Error("项目名请使用小写字母、数字、连字符及下划线，以字母或数字开头。")
        return args.project
    records = []
    for path in (ROOT / ".zentaoctl").glob("*/release.json"):
        record = read_json(path)
        if record.get("engine", {}).get("id") == engine["id"]:
            records.append(record["project"])
    projects = sorted(set(records) | set(site.discover()))
    if len(records) == 1:
        local = site.projects_in_directory(ROOT)
        if not (local - set(records)):
            return records[0]
    print("可选 Compose 项目：" + (", ".join(projects) if projects else "暂无"))
    if args.command == "status":
        for project in projects:
            print(f"  ./scripts/zentaoctl status --project {shlex.quote(project)}")
        return None
    raise Error("请通过 --project 明确指定本次维护的实例。")


def assert_private_env(ctx):
    for path in (ctx.root / ".zentaoctl").glob("*/*.json"):
        if path.name not in ("release.json", "operation.json") or path.parent == ctx.state_dir:
            continue
        other = read_json(path)
        if other and other.get("env_file") == str(ctx.env_file):
            raise Error(f"环境文件已归属于 {path.parent.name}，请使用独立的 --env-file。")
    # Manual deployments have no release.json. Check their original env selection too.
    rows = ctx.docker("ps", "-a", "--filter", "label=com.docker.compose.project",
                      "--format", '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Label "com.docker.compose.project.environment_file"}}')
    for row in rows.splitlines():
        project, directory, envs = row.split("|", 2)
        if project == ctx.project or not directory:
            continue
        paths = [Path(directory) / p for p in envs.split(",")] if envs else [Path(directory) / ".env"]
        if ctx.env_file in [p.resolve() for p in paths]:
            raise Error(f"现有项目 {project} 也引用此环境文件。请为每个实例选择独立的 --env-file。")


def main(argv=None):
    args = parser().parse_args(argv)
    pinned_context = False
    try:
        engine = site.engine_identity()
        if not os.environ.get("DOCKER_HOST") and "DOCKER_CONTEXT" not in os.environ:
            # An unrelated "docker context use" must not redirect a running maintenance job.
            os.environ["DOCKER_CONTEXT"] = engine["context"]
            pinned_context = True
            if site.engine_identity() != engine:
                raise Error("Docker 端点在检查期间已改变，请重新核对实例。")
        project = select_project(args, engine)
        if project is None:
            return 0
        read_only = args.command == "status" or getattr(args, "dry_run", False)
        lock = contextlib.nullcontext() if read_only else project_lock(engine, project)
        with lock, contextlib.ExitStack() as locks:
            ctx = Context(ROOT, project, args.env_file, engine)
            overrides = sorted(k for k in os.environ if k.startswith(("COMPOSE_", "ZENTAO_", "ZT_", "MYSQL_")))
            if overrides:
                print("以下 shell 变量已忽略，部署配置从所选环境文件读取：" + ", ".join(overrides))
            backup_job = read_json(ctx.state_dir / "backup-interrupted.json")
            if backup_job:
                owner = intent_command(ctx.intent) if ctx.intent else "backup"
                resume = (f"backup --project {ctx.project} --output {shlex.quote(backup_job['output'])}"
                          if owner == "backup" else f"{owner} --project {ctx.project}（沿用原参数）")
                print("上次冷备尚未恢复并验收全部服务，可续跑 " + resume)
                if args.command not in (owner, "status"):
                    raise Error("请先完成上述冷备恢复。")
            if ctx.intent and not read_only and intent_command(ctx.intent) != args.command:
                raise Error("有未完成操作，请先核对 status，再续跑 " + intent_command(ctx.intent))
            needs_http = {"adopt", "recover", "restart", "install", "harden", "configure",
                          "update", "backup", "reinstall"}
            if (args.command in needs_http or getattr(args, "verify", False)) and not engine["endpoint"].startswith("unix://"):
                raise Error("此操作需要从部署主机的本地 Docker 端点验收 HTTP 入口，请在部署主机运行。")
            if args.command == "status":
                return status(ctx, args)
            if args.command in ("install", "configure", "update", "harden"):
                if not read_only:
                    locks.enter_context(project_lock({"id": "shared-env-file"}, str(ctx.env_file)))
                assert_private_env(ctx)
            if args.command in ("install", "harden"):
                from . import install
                getattr(install, args.command)(ctx, args)
            elif args.command in ("configure", "update", "reinstall"):
                from . import change
                change.handle(ctx, args)
            elif args.command in ("backup", "restore", "uninstall", "purge-data"):
                from . import dataops
                dataops.handle(ctx, args)
            else:
                existing(ctx, args)
        return 0
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"zentaoctl: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作已中断。请查看 status 后重试。", file=sys.stderr)
        return 130
    finally:
        if pinned_context:
            os.environ.pop("DOCKER_CONTEXT", None)
