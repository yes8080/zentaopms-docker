"""Prepare the three host directories used by compose.persistence.yaml."""

import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path


_ROLES = ("mysql", "config", "data")
_DIRECTORY = os.O_DIRECTORY | os.O_NOFOLLOW


def _absolute_root(path):
    root = Path(path)
    if not root.is_absolute() or root == Path(root.anchor) or ".." in root.parts:
        raise ValueError(f"新站数据目录必须是独立的绝对路径：{root}")
    return root


def _open_root(root, create):
    """Open every component relative to the preceding directory; never follow a link."""
    flags = _DIRECTORY | os.O_PATH
    fd = os.open(root.anchor, flags)
    try:
        for i, component in enumerate(root.parts[1:], start=1):
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    return None
                mode = 0o700 if i == len(root.parts) - 1 else 0o755
                try:
                    os.mkdir(component, mode, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.dup(fd)
    except NotADirectoryError as error:
        raise ValueError(f"数据目录路径含符号链接或非目录项：{root}") from error
    finally:
        os.close(fd)


def _identity(info):
    return [info.st_dev, info.st_ino]


def _inspect(root, fd, resumed):
    observed = dict.fromkeys(("root", *_ROLES))
    observed["path"] = str(root)
    if fd is None:
        return observed
    observed["root"] = _identity(os.fstat(fd))
    if not resumed:
        reader = os.open(".", _DIRECTORY | os.O_RDONLY, dir_fd=fd)
        try:
            with os.scandir(reader) as entries:
                if any(entry.name not in _ROLES for entry in entries):
                    raise ValueError(f"新站数据根含已有文件，请使用专属空目录：{root}")
        finally:
            os.close(reader)
    for name in _ROLES:
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"持久化路径必须是实际目录：{root / name}")
        observed[name] = _identity(info)
        if not resumed:
            child = os.open(name, _DIRECTORY | os.O_RDONLY, dir_fd=fd)
            try:
                with os.scandir(child) as entries:
                    if next(entries, None) is not None:
                        raise ValueError(f"发现已有数据，已停止新站安装：{root / name}")
            finally:
                os.close(child)
    return observed


def check_bind_storage(path, *, resumed=False, identity=None):
    """Return directory identities without creating paths. Missing fresh paths have value None."""
    root = _absolute_root(path)
    fd = _open_root(root, False)
    try:
        observed = _inspect(root, fd, resumed)
    finally:
        if fd is not None:
            os.close(fd)
    if resumed:
        if not isinstance(identity, dict) or identity.get("path") != str(root):
            raise ValueError(f"缺少该路径的安装登记，请核对原数据目录：{root}")
        for key in ("root", *_ROLES):
            if observed[key] is None or identity.get(key) != observed[key]:
                target = root if key == "root" else root / key
                raise ValueError(f"已登记的数据目录缺失或身份改变：{target}")
    return observed


def prepare_bind_storage(path):
    """Create missing empty directories. Store the result before starting any containers."""
    root = _absolute_root(path)
    fd = _open_root(root, True)
    try:
        present = _inspect(root, fd, False)
        for name in _ROLES:
            if present[name] is None:
                os.mkdir(name, 0o755, dir_fd=fd)
        prepared = _inspect(root, fd, False)
    finally:
        os.close(fd)
    return check_bind_storage(root, resumed=True, identity=prepared)


_BIND_ROLES = {"host-mysql-data": "mysql", "app-config": "config", "host-app-data": "data"}


def _verify_probe_volume(ctx, name, options, labels):
    volume = json.loads(ctx.docker("volume", "inspect", "--", name))[0]
    found = volume.get("Labels") or {}
    if (volume.get("Name") != name or volume.get("Driver") != "local" or
            volume.get("Options") != options or any(found.get(key) != value for key, value in labels.items())):
        raise ValueError(f"实际卷 {name} 与本次安装登记的来源、驱动或标签不符。")
    if ctx.docker("ps", "-aq", "--filter", "volume=" + name).strip():
        raise ValueError(f"实际卷 {name} 正被现有容器引用，无法确认为新站空存储。")


def _drop_marker(fd, name, identity):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _identity(info) != identity:
        raise ValueError("本机映射探针已被替换，已保留文件并暂停安装。")
    os.unlink(name, dir_fd=fd)


def _place_marker(fd, role, token, cleanup):
    """An interrupted run can recognize and finish its own partially written marker."""
    name = ".zentaoctl-" + hashlib.sha256(f"marker:{token}:{role}".encode()).hexdigest()
    secret = hashlib.sha256(f"content:{token}:{role}".encode()).hexdigest()
    with os.scandir(fd) as entries:
        if any(entry.name != name for entry in entries):
            raise ValueError(f"{role} 目录出现已有文件，已停止新装。")
    try:
        marker = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    except FileExistsError:
        marker = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(marker, "r+b") as stream:
        info = os.fstat(marker)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or not secret.encode().startswith(stream.read(len(secret) + 1))):
            raise ValueError(f"{role} 中发现无法确认归属的标记，已保留原文件。")
        cleanup.callback(_drop_marker, fd, name, _identity(info))
        stream.seek(0)
        stream.write(secret.encode())
        stream.flush()
        # This disposable value must be readable from both ordinary and rootless mounts.
        os.fchmod(marker, 0o644)
    return name, secret


def prove_bind_volumes(ctx, model, volumes, data, php_image, token):
    """Prove the real local-driver destinations before any service or volume copy-up."""
    root = _absolute_root(data)
    if not re.fullmatch(r"[a-f0-9]{32,64}", token):
        raise ValueError("缺少可验证的绑定安装标记。")
    recorded = (ctx.intent or {}).get("identity")
    check_bind_storage(root, resumed=True, identity=recorded)
    if set(volumes) != set(_BIND_ROLES) or len(set(volumes.values())) != len(_BIND_ROLES):
        raise ValueError("新装的三个绑定数据卷必须独立。")
    declared = {}
    existing = set(ctx.docker("volume", "ls", "-q").splitlines())
    for logical, role in _BIND_ROLES.items():
        spec = model["volumes"][logical]
        name = volumes[logical]
        opts = {"type": "none", "o": "bind", "device": str(root / role)}
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or spec.get("name") != name or
                spec.get("driver", "local") != "local" or spec.get("external") or spec.get("driver_opts") != opts):
            raise ValueError(f"数据卷 {logical} 的实际目标位置或驱动不符合安装设置。")
        labels = dict(spec.get("labels") or {})
        if labels.get("io.zentaoctl.install") != token:
            raise ValueError(f"{logical} 缺少本次安装的卷标记。")
        labels.update({"com.docker.compose.project": ctx.project, "com.docker.compose.volume": logical})
        declared[name] = opts, labels
        if name in existing:
            _verify_probe_volume(ctx, name, opts, labels)

    for name, (options, labels) in declared.items():
        if name not in existing:
            args = ["volume", "create", "--driver", "local"]
            for k, v in options.items():
                args.extend(("--opt", f"{k}={v}"))
            for k, v in labels.items():
                args.extend(("--label", f"{k}={v}"))
            ctx.docker(*args, "--", name)
        _verify_probe_volume(ctx, name, options, labels)

    with ExitStack() as cleanup:
        root_fd = _open_root(root, False)
        if root_fd is None:
            raise ValueError(f"原数据目录缺失：{root}")
        cleanup.callback(os.close, root_fd)
        if _identity(os.fstat(root_fd)) != recorded["root"]:
            raise ValueError(f"原数据目录已被替换：{root}")
        markers = {}
        mounts = []
        for logical, role in _BIND_ROLES.items():
            fd = os.open(role, _DIRECTORY | os.O_RDONLY, dir_fd=root_fd)
            cleanup.callback(os.close, fd)
            if _identity(os.fstat(fd)) != recorded[role]:
                raise ValueError(f"{role} 数据目录身份已改变。")
            markers[role] = _place_marker(fd, role, token, cleanup)
            mounts.extend(("--mount", f"type=volume,src={volumes[logical]},dst=/proof/{role},readonly,volume-nocopy"))

        probe = """
        $inodes = [];
        foreach(json_decode($argv[1], true) as $role => [$marker, $expected]) {
            $dir = '/proof/' . $role;
            $file = $dir . '/' . $marker;
            $entries = array_values(array_diff(scandir($dir), ['.', '..']));
            if ($entries !== [$marker] || is_link($file) || !is_file($file) ||
                !hash_equals($expected, (string)file_get_contents($file))) exit(1);
            $inode = fileinode($dir);
            if ($inode === false) exit(1);
            $inodes[$role] = sprintf('%u', $inode);
        }
        echo json_encode($inodes);
        """
        raw = ctx.docker("run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                         "--user", "0", "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE",
                         "--security-opt", "no-new-privileges", *mounts, "--entrypoint", "php", php_image,
                         "-n", "-r", probe, json.dumps(markers))
        inodes = json.loads(raw)
        if set(inodes) != set(_ROLES) or any(not isinstance(i, str) or not i.isdecimal() for i in inodes.values()):
            raise ValueError("无法验证 Docker 引擎端数据目录身份。")
    if check_bind_storage(root) != recorded:
        raise ValueError("绑定路径在验证期间变化，请检查原存储。")
    return {volumes[logical]: inodes[role] for logical, role in _BIND_ROLES.items()}
