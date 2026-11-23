"""Remove only individually confirmed, previously witnessed local Compose data directories."""
import contextlib
import json
import os
import re
import secrets
import stat
from pathlib import Path

from .common import Error
from .dataops import ROLES, validate_backup
from .fresh_storage import _open_root


DIR = os.O_DIRECTORY | os.O_NOFOLLOW
ROLES_TO_PURGE = ("mysql", "config", "data")


def _overlap(first, second):
    return first == second or first in second.parents or second in first.parents


def _id(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _mount_id(fd):
    text = Path(f"/proc/self/fdinfo/{fd}").read_text()
    match = re.search(r"^mnt_id:\s*(\d+)$", text, re.M)
    if not match:
        raise Error("无法从本机 Linux 内核确认挂载边界。")
    return match[1]


def _stable_entry(parent, name, fd):
    entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _id(fd) != (entry.st_dev, entry.st_ino):
        raise Error("目录项在检查期间发生变化，已停止。")


def _walk(fd, mount, remove=False):
    """Use directory fds throughout; also catch bind mounts on the same filesystem."""
    if _mount_id(fd) != mount:
        raise Error("数据目录包含独立挂载，已保留。")
    if not os.access(".", os.W_OK | os.X_OK, dir_fd=fd, effective_ids=True):
        raise Error("执行账号无法清理目录，请在部署主机核对权限。")
    for name in os.listdir(fd):
        item = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=fd)
        try:
            mode = os.fstat(item).st_mode
            if stat.S_ISLNK(mode) or _mount_id(item) != mount:
                raise Error("数据含符号链接或嵌套挂载，已停止。")
            if stat.S_ISDIR(mode):
                child = os.open(".", DIR | os.O_RDONLY, dir_fd=item)
                try:
                    _walk(child, mount, remove)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(mode):
                raise Error("数据含非普通文件，已停止。")
            if remove:
                _stable_entry(fd, name, item)
                if stat.S_ISDIR(mode):
                    os.rmdir(name, dir_fd=fd)
                else:
                    os.unlink(name, dir_fd=fd)
        finally:
            os.close(item)


def _candidates(ctx):
    model = ctx.model()
    env_root = ctx.settings().get("ZENTAO_DATA_DIR", "")
    root = Path(env_root)
    if not root.is_absolute() or str(root) != env_root or len(root.parts) < 2 or ".." in root.parts:
        raise Error("未找到规范的 ZENTAO_DATA_DIR，无法确认宿主机存储根。")
    mounts = ctx.record["evidence"]["mounts"]
    found = {}
    for role in ROLES_TO_PURGE:
        service, target = ROLES[role]
        path = root / role
        recorded = mounts.get(service, {}).get(target) or {}
        expected = next((m for m in model["services"][service]["volumes"] if m["target"] == target), {})
        definition = model.get("volumes", {}).get(expected.get("source"), {})
        if (recorded.get("type") != "volume" or recorded.get("driver") != "local" or
                recorded.get("device") != str(path) or recorded.get("source") != str(path) or
                definition.get("external") or not recorded.get("created") or
                definition.get("name") != recorded.get("name") or not expected.get("source") or
                definition.get("driver_opts") != {"type": "none", "o": "bind", "device": str(path)}):
            raise Error(f"{target} 的持久化定义或登记来源不符，保留宿主机目录。")
        found[str(path)] = (role, target, recorded, expected["source"])
    return found


def _filesystem_location(path, mounts):
    """Map a host path to a filesystem-relative path, including bind aliases."""
    resolved = path.resolve()
    matching = [(len(m.parts), device, base / resolved.relative_to(m)) for m, device, base in mounts
                if m == resolved or m in resolved.parents]
    if not matching:
        raise Error("无法确认 Docker 绑定路径对应的本机文件系统。")
    _, device, relative = max(matching, key=lambda x: x[0])
    return device, relative


def _host_mounts():
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)

    rows = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        rows.append((Path(unescape(fields[4])), fields[2], Path(unescape(fields[3]))))
    return rows


def _same_storage(first, second, table):
    if _overlap(first.resolve(), second.resolve()):
        return True
    device_a, path_a = _filesystem_location(first, table)
    device_b, path_b = _filesystem_location(second, table)
    return device_a == device_b and _overlap(path_a, path_b)


def _inventory(ctx, candidates, selected):
    # Docker's regular mounts include stopped containers. Other volume definitions can
    # claim a path even when no container currently references that volume.
    ids = ctx.docker("ps", "-aq").split()
    rows = ctx.docker("container", "inspect", "--format",
                      '{{index .Config.Labels "com.docker.compose.project"}}|{{json .Mounts}}',
                      "--", *ids).splitlines() if ids else []
    names = ctx.docker("volume", "ls", "-q").split()
    volumes = json.loads(ctx.docker("volume", "inspect", "--", *names)) if names else []
    by_name = {v["Name"]: v for v in volumes}

    host_mounts = _host_mounts()

    def overlaps(device, wanted):
        path = Path(device)
        if not path.is_absolute():
            raise Error("其他卷的本地来源无法解析。")
        return _same_storage(path, wanted, host_mounts)

    for selected_path in selected:
        path = Path(selected_path)
        _, _, m, logical = candidates[selected_path]
        name = m["name"]
        current = by_name.get(name) or {}
        labels = current.get("Labels") or {}
        if (current.get("Driver") != "local" or current.get("CreatedAt") != m["created"] or
                labels.get("com.docker.compose.project") != ctx.project or
                labels.get("com.docker.compose.volume") != logical or
                current.get("Options") != {"type": "none", "o": "bind", "device": selected_path}):
            raise Error(f"{path} 对应卷的归属、创建记录或挂载方式已变化。")
        sources = [path]
        if current.get("Mountpoint"):
            sources.append(Path(current["Mountpoint"]))
        for row in rows:
            proj, raw = row.split("|", 1)
            if proj == ctx.project:
                raise Error("项目仍有容器，请先卸载。")
            for other in json.loads(raw):
                if other.get("Name") == name or (other.get("Type") == "bind" and
                        any(overlaps(other["Source"], p) for p in sources)):
                    raise Error(f"{path} 仍被容器挂载或与其绑定路径重叠。")
        for volume in volumes:
            if volume["Name"] == name:
                continue
            opts = volume.get("Options") or {}
            if volume.get("Driver") != "local":
                raise Error("当前引擎存在无法核实底层路径的外部卷驱动。")
            if ({"bind", "rbind"}.intersection(opts.get("o", "").split(",")) and
                    any(overlaps(opts.get("device", ""), p) for p in sources)):
                raise Error(f"{path} 和另一个 Docker 卷的绑定目录重叠。")


def _verify_host(ctx, candidates, selected, selected_fds):
    # Prove that this local kernel and the daemon see the same selected data through
    # a read-only volume. A Unix socket alone can still point at a Docker Desktop VM.
    images = ctx.record["evidence"]["images"]
    witnesses = ctx.record["witnesses"]
    local_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f-]{36}", local_boot):
        raise Error("无法确认本机内核身份。")
    local_uuid = None
    for selected_path in selected:
        role, target, recorded, _ = candidates[selected_path]
        device, inode = _id(selected_fds[selected_path])
        args = ("run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE", "--security-opt", "no-new-privileges",
                "--user", "0", "--mount", "type=volume,src=" + recorded["name"] + ",dst=/check,readonly,volume-nocopy",
                "--entrypoint", "sh", images["nginx"], "-ec")
        script = 'cat /proc/sys/kernel/random/boot_id; stat -c "%d:%i" /check'
        if role == "mysql":
            script += """; awk -F= '/^server-uuid=/{print $2}' /check/auto.cnf"""
        observed = ctx.docker(*args, script).splitlines()
        if (observed[:2] != [local_boot, f"{device}:{inode}"] or
                str(inode) != witnesses.get(target)):
            raise Error(f"{selected_path} 的本机、Docker 引擎与原登记身份不一致。")
        if role == "mysql":
            local_uuid = observed[2] if len(observed) == 3 else ""
    if local_uuid is None:
        # The database must still be present to attribute other role directories.
        db = next(v[2] for v in candidates.values() if v[0] == "mysql")
        if db["name"] not in ctx.docker("volume", "ls", "-q").splitlines():
            raise Error("原数据库卷已缺失，无法核实站点 UUID。")
        local_uuid = ctx.docker("run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                                "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE",
                                "--security-opt", "no-new-privileges", "--user", "0",
                                "--mount", f'type=volume,src={db["name"]},dst=/check,readonly,volume-nocopy',
                                "--entrypoint", "awk", images["nginx"], "-F=", '/^server-uuid=/{print $2}', "/check/auto.cnf").strip()
    if not local_uuid or local_uuid != witnesses.get("mysql_uuid"):
        raise Error("无法核实原站点的 MySQL UUID。")


def _remove_confirmed(parent, role, fd, mount, release):
    # Detach the confirmed tree under a private name before traversing it. A later
    # deployment reusing the old path cannot be touched by this operation.
    _stable_entry(parent, role, fd)
    quarantine = ".zentaoctl-purge-" + secrets.token_hex(12)
    os.mkdir(quarantine, mode=0o700, dir_fd=parent)
    private = os.open(quarantine, DIR, dir_fd=parent)
    try:
        os.rename(role, "confirmed", src_dir_fd=parent, dst_dir_fd=private)
        _stable_entry(private, "confirmed", fd)
        release()
        _walk(fd, mount, remove=True)
        os.rmdir("confirmed", dir_fd=private)
    except (OSError, Error) as exc:
        raise Error(f"清理中断，请保留并核对临时目录 {quarantine}。") from exc
    finally:
        os.close(private)
    os.rmdir(quarantine, dir_fd=parent)


def purge_bind(ctx, args):
    selected = list(dict.fromkeys(args.confirm_dir))
    if not selected or (not args.dry_run and args.confirm_project != ctx.project):
        raise Error("请逐项提供 --confirm-dir 和准确的 --confirm-project。")
    if not args.from_backup or not ctx.engine.get("endpoint", "").startswith("unix://"):
        raise Error("宿主目录清理需要本地 Docker 引擎和 --from-backup 指定的原站冷备。")
    candidates = _candidates(ctx)
    if any(s not in candidates for s in selected):
        raise Error("只接受登记的 mysql、config 或 data 角色目录的完整绝对路径。")
    root = Path(selected[0]).parent
    backup = Path(args.from_backup).resolve(strict=True)
    protected = [backup, ctx.root, ctx.state_dir, ctx.env_file,
                 Path(ctx.docker("info", "--format", "{{.DockerRootDir}}").strip())]
    host_mounts = _host_mounts()
    if _same_storage(root, backup, host_mounts) or any(
            _same_storage(Path(s), p, host_mounts) for s in selected for p in protected):
        raise Error("目标数据路径与备份、部署文件或 Docker 存储重叠。")
    manifest = validate_backup(backup)
    saved = manifest.get("site") or {}
    if (manifest.get("project") != ctx.project or saved.get("engine") != ctx.record.get("engine") or
            not saved.get("witnesses") or saved["witnesses"] != ctx.record["witnesses"] or
            saved.get("evidence", {}).get("mounts") != ctx.record["evidence"]["mounts"]):
        raise Error("冷备与该实例的引擎、卷或数据身份不符。")
    _inventory(ctx, candidates, selected)
    with contextlib.ExitStack() as stack:
        parent = _open_root(root, False)
        if parent is None:
            raise Error("原数据根目录不存在。")
        stack.callback(os.close, parent)
        fds = {}
        mount = _mount_id(parent)
        for s in selected:
            fd = os.open(Path(s).name, DIR | os.O_RDONLY, dir_fd=parent)
            stack.callback(os.close, fd)
            fds[s] = fd
            _walk(fd, mount)
        _verify_host(ctx, candidates, selected, fds)
        _inventory(ctx, candidates, selected)
        if not os.access(".", os.W_OK | os.X_OK, dir_fd=parent, effective_ids=True):
            raise Error("执行账号无法管理已核实的数据根目录。")
        print("已核对备份及原卷，待永久删除的角色目录：" + "、".join(selected))
        if args.dry_run:
            return
        for s in sorted(selected, key=lambda p: candidates[p][0] == "mysql"):
            role, _, recorded, _ = candidates[s]
            _inventory(ctx, candidates, [s])
            def release():
                # Recheck references after detaching the path. The engine also blocks
                # volume removal if a container acquired a reference in the interim.
                _inventory(ctx, candidates, [s])
                ctx.docker("volume", "rm", "--", recorded["name"])
            _remove_confirmed(parent, role, fds[s], mount, release)
            print(f"已删除 {s} 和对应的绑定卷定义。")
