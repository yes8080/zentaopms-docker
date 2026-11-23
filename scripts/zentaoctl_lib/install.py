"""New installations and normal-to-hardened transitions with resumable, private image tags."""
import copy
import errno
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .common import Error, atomic_write, require_clean
from .env_edit import render_env, write_env
from .fresh_storage import check_bind_storage, prepare_bind_storage


def without_candidates(ctx, files):
    return [Path(f) for f in files if not (Path(f).parent == ctx.state_dir
            and re.fullmatch(r"images-[a-f0-9]{32}\.yaml", Path(f).name))]


def _revision(repo):
    revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
                              text=True, capture_output=True)
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=normal"],
                            text=True, capture_output=True)
    if revision.returncode or status.returncode or status.stdout.strip():
        return "unknown"
    return revision.stdout.strip()


def _candidate(ctx, mode, cache, token):
    if mode not in ("normal", "hardened") or cache not in ("redis", "apcu", "none"):
        raise Error("无法识别候选镜像模式或缓存。")
    revisions = {"io.zentaoctl.app-revision": _revision(ctx.root.parent / "zentaopms"),
                 "io.zentaoctl.deployment-revision": _revision(ctx.root)}
    services = {}
    for service in ("php", "nginx"):
        labels = {"io.zentaoctl.mode": mode, "io.zentaoctl.service": service,
                  "io.zentaoctl.candidate": token, "io.zentaoctl.cache": cache, **revisions}
        services[service] = {"image": f"zentaoctl/{ctx.project}-{service}:{mode}-{token}",
                             "build": {"labels": labels}}
    return {"services": services}


def _build(ctx, files, mode, cache, token):
    overlay = ctx.state_dir / f"images-{token}.yaml"
    spec = _candidate(ctx, mode, cache, token)
    atomic_write(overlay, json.dumps(spec, indent=2) + "\n")
    files = without_candidates(ctx, files) + [overlay]
    services = ctx.model(files)["services"]
    for name in ("php", "nginx"):
        expected = name if mode == "normal" else f"{name}-hardened"
        if services[name]["build"].get("target") != expected:
            raise Error(f"{name} 的构建目标与 {mode} 模式不符。")
    ctx.dc("build", "php", "nginx", files=files)
    for svc in spec["services"].values():
        labels = json.loads(ctx.docker("image", "inspect", "--format", "{{json .Config.Labels}}", svc["image"]))
        if any(labels.get(k) != v for k, v in svc["build"]["labels"].items()):
            raise Error("候选镜像标记不符，原站继续使用现有镜像。")
    return files


def build_candidates(ctx, files, *, mode, cache):
    return _build(ctx, files, mode, cache, secrets.token_hex(16))


def _path_list(ctx, files):
    sep = (ctx.settings().get("COMPOSE_PATH_SEPARATOR") if ctx.env_file.exists() else None) or os.pathsep
    if any(sep in str(f) for f in files):
        raise Error("Compose 文件路径含当前列表分隔符。")
    return sep.join(str(f) for f in files)


# Run in PHP's own container to query the real configured application database. Suppress
# messages from the config/PDO; credentials and stack traces never leave the process.
_VERSION = r"""
error_reporting(0); ini_set('display_errors', '0'); ob_start();
try {
    chdir('/var/www/zentao');
    if(!is_file('config/my.php') || !filesize('config/my.php')) {ob_end_clean(); echo 'pending'; exit;}
    define('DS', DIRECTORY_SEPARATOR);
    require_once 'framework/base/helper.class.php';
    class helper extends baseHelper {}
    $config = (object)['db'=>(object)[], 'default'=>(object)[]]; $slaveDB = (object)[];
    include 'config/my.php';
    if(empty($config->installed)) {ob_end_clean(); echo 'pending'; exit;}
    $d=$config->db; $prefix=$d->prefix ?? getenv('ZT_DB_PREFIX');
    if($prefix === false) $prefix='zt_';
    if(!preg_match('/^[A-Za-z0-9_]*$/D', $prefix)) exit(2);
    $pdo=new PDO('mysql:host='.$d->host.';port='.($d->port ?: 3306).';dbname='.$d->name,
        $d->user, $d->password, [PDO::ATTR_TIMEOUT=>4,PDO::ATTR_ERRMODE=>PDO::ERRMODE_EXCEPTION]);
    if(($argv[1] ?? '') === 'identity') {
        $identity=$pdo->query('SELECT @@server_uuid AS uuid, DATABASE() AS name')->fetch(PDO::FETCH_ASSOC);
        $identity['prefix']=$prefix; ob_end_clean(); echo json_encode($identity); exit;
    }
    $table=$prefix.'config';
    $exists=$pdo->prepare('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=?');
    $exists->execute([$table]);
    $value=false;
    if($exists->fetchColumn()) $value=$pdo->query("SELECT value FROM `$table` WHERE owner='system' AND module='common' AND section='global' AND `key`='version' LIMIT 1")->fetchColumn();
    ob_end_clean();
    echo $value !== false && $value !== '' ? 'version:'.$value : 'pending';
} catch(Throwable $e) {while(ob_get_level()) ob_end_clean(); exit(2);}
"""


def installed_version(ctx, files=None):
    out = ctx.dc("exec", "-T", "php", "php", "-r", _VERSION, files=files).strip()
    if out == "pending":
        return None
    if not re.fullmatch(r"version:[A-Za-z0-9_.+-]{1,96}", out):
        raise Error("无法确认数据库中的禅道版本，请检查实际数据库和安装进度。")
    return out.split(":", 1)[1]


def database_identity(ctx, files=None):
    out = ctx.dc("exec", "-T", "php", "php", "-r", _VERSION, "identity", files=files).strip()
    try:
        value = json.loads(out)
    except ValueError:
        raise Error("无法确认 PHP 使用的实际数据库身份。") from None
    if (not isinstance(value, dict) or
            not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value.get("uuid", "")) or
            not value.get("name") or not isinstance(value.get("prefix"), str)):
        raise Error("未能核实 PHP 的数据库标识及表前缀。")
    return value


@contextmanager
def preview_env(ctx, text):
    # Compose parses the actual prospective dotenv with its own interpolation rules.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="zt-install-") as handle:
        handle.write(text)
        handle.flush()
        view = copy.copy(ctx)
        view.env_file = Path(handle.name)
        yield view


def _cache(model):
    return model["services"]["php"].get("environment", {}).get("ZT_CACHE_DRIVER") or "none"


def _layout(ctx, args):
    settings = ctx.settings() if ctx.env_file.exists() else {}
    if settings.get("COMPOSE_PROJECT_NAME") not in (None, "", ctx.project):
        raise Error("环境文件已指定另一个 Compose 项目。请使用独立环境文件。")
    files = ctx.files() if ctx.env_file.exists() else [ctx.root / "compose.yaml"]
    existing = settings.get("COMPOSE_FILE")
    known = {p.name for p in files if p.parent == ctx.root}
    if "compose.hardened.yaml" in known:
        raise Error("新安装必须先使用普通镜像。已有站点请先 adopt。")
    current_storage = "bind" if "compose.persistence.yaml" in known else "volume"
    current_cache = "redis" if "compose.redis.yaml" in known else "apcu" if "compose.apcu.yaml" in known else "none"
    storage, cache = args.storage or current_storage, args.cache or current_cache
    if existing and (storage != current_storage or cache != current_cache):
        raise Error("命令选项与环境文件现有的 Compose 文件集不符，请核对实例。")
    if storage == "bind" and current_storage != "bind":
        files.append(ctx.root / "compose.persistence.yaml")
    if cache != "none" and cache != current_cache:
        files.append(ctx.root / f"compose.{cache}.yaml")
    value = str(args.data_dir or settings.get("ZENTAO_DATA_DIR") or "")
    if storage == "bind" and (not value or not Path(value).is_absolute()):
        raise Error("宿主机绑定存储需要绝对路径 --data-dir。")
    if storage == "volume":
        if args.data_dir:
            raise Error("--data-dir 仅适用于 --storage bind。")
        value = ""
    if settings.get("ZENTAO_DATA_DIR") and args.data_dir and Path(value) != Path(settings["ZENTAO_DATA_DIR"]):
        raise Error("数据目录与现有环境文件不符。")
    try:
        port = int(args.port if args.port is not None else settings.get("ZENTAO_PORT") or 8080)
    except ValueError:
        raise Error("端口必须是 1–65535 的整数。") from None
    if not 1 <= port <= 65535:
        raise Error("端口必须是 1–65535 的整数。")
    updates = dict(COMPOSE_PROJECT_NAME=ctx.project, COMPOSE_FILE=_path_list(ctx, files),
                   ZENTAO_PORT=str(port))
    if not settings.get("ZENTAO_BIND"):
        updates["ZENTAO_BIND"] = "127.0.0.1"
    if storage == "bind":
        updates["ZENTAO_DATA_DIR"] = value
    if cache == "redis" and settings.get("ZT_REDIS_PASSWORD") and not re.fullmatch(r"[0-9a-fA-F]{32,}", settings["ZT_REDIS_PASSWORD"]):
        raise Error("ZT_REDIS_PASSWORD 至少需要 32 位十六进制；请在本实例环境文件中更正。")
    for key in ["MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD"] + (["ZT_REDIS_PASSWORD"] if cache == "redis" else []):
        if not settings.get(key):
            updates[key] = secrets.token_hex(32)
    return files, storage, cache, value, port, updates


def _volume_list(ctx, model):
    active = {m["source"] for s in model["services"].values() for m in s.get("volumes", [])
              if m["type"] == "volume"}
    volumes = {}
    for logical in active:
        v = model["volumes"][logical]
        if v.get("external") or v.get("driver", "local") != "local":
            raise Error("新安装发现外部或非本地卷，请核对数据并接管已有实例。")
        volumes[logical] = v["name"]
    return volumes


def _check_scope(ctx, model, storage, data, cache):
    svc = model["services"]
    if set(svc) != {"php", "nginx", "mysql"} | ({"redis"} if cache == "redis" else set()) or _cache(model) != cache:
        raise Error("实际 Compose 服务或缓存与新装方案不符。")
    for name in ("php", "nginx"):
        if svc[name].get("build", {}).get("target") != name:
            raise Error(f"安装阶段必须使用普通的 {name} 构建目标。")
    ports = svc["nginx"].get("ports", [])
    if not ports or any(p.get("host_ip") not in ("127.0.0.1", "::1") for p in ports):
        raise Error("新安装向导需要回环地址；请先将 ZENTAO_BIND 设置为 127.0.0.1 或 ::1。")
    expected = {"mysql-data", "app-config", "app-data"} if storage == "volume" else {"host-mysql-data", "app-config", "host-app-data"}
    vols = _volume_list(ctx, model)
    if set(vols) != expected:
        raise Error("实际数据卷与新安装所选存储不符。")
    for s in svc.values():
        if any(m.get("type") == "bind" for m in s.get("volumes", [])):
            raise Error("新装模板发现额外直接绑定目录，请先核对其归属。")
    devices = {"host-mysql-data": "mysql", "app-config": "config", "host-app-data": "data"}
    for name in vols:
        opts = model["volumes"][name].get("driver_opts") or {}
        if storage == "bind":
            if opts.get("type") != "none" or opts.get("o") != "bind" or opts.get("device") != str(Path(data) / devices[name]):
                raise Error(f"卷 {name} 的实际绑定目录与目标不符。")
        elif opts:
            raise Error(f"卷 {name} 包含意外的驱动参数。")
    return vols


def _check_ports(ctx, model):
    bindings = model["services"]["nginx"].get("ports", [])
    # Inspect the daemon's published ports even when a different Docker context is selected.
    ids = ctx.docker("ps", "-q").splitlines()
    rows = []
    if ids:
        fmt = '{{index .Config.Labels "com.docker.compose.project"}}|{{json .NetworkSettings.Ports}}'
        rows = ctx.docker("inspect", "--format", fmt, *ids).splitlines()
    for binding in bindings:
        host, port = binding["host_ip"], int(binding["published"])
        ours = False
        for row in rows:
            owner, _, mappings = row.partition("|")
            for addresses in (json.loads(mappings) or {}).values():
                for item in addresses or []:
                    if int(item["HostPort"]) != port or item["HostIp"] not in ("", "0.0.0.0", "::", host):
                        continue
                    if owner != ctx.project:
                        raise Error(f"宿主机端口 {host}:{port} 已被其他容器使用，请选择独立 --port。")
                    ours = True
        if not ours and ctx.engine["endpoint"].startswith("unix://"):
            try:
                with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
                    probe.bind((host, port))
            except OSError as err:
                if err.errno == errno.EADDRINUSE:
                    raise Error(f"宿主机端口 {host}:{port} 已被占用，请另选 --port。") from None
                raise Error(f"无法核对宿主监听地址 {host}:{port}。") from None


def _check_volume_owner(ctx, model, volumes, token=None):
    all_names = set(ctx.docker("volume", "ls", "-q").splitlines())
    owned = set(ctx.docker("volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={ctx.project}").splitlines())
    if owned - set(volumes.values()):
        raise Error("此项目已有未登记卷。保留现有数据，先检查 status/adopt。")
    for key, name in volumes.items():
        if name not in all_names:
            continue
        if not token:
            raise Error(f"目标卷 {name} 已经存在，请检查已有数据并使用 adopt。")
        mark = ctx.docker("volume", "inspect", "--format", '{{index .Labels "io.zentaoctl.install"}}', name).strip()
        if mark != token:
            raise Error(f"卷 {name} 与此次安装意图不符。")


def _check_foreign_devices(ctx, path, allowed):
    def overlaps(device):
        if not device.startswith("/"):
            return False
        other = Path(device).resolve()
        return other == path or path in other.parents or other in path.parents

    names = ctx.docker("volume", "ls", "-q").splitlines()
    for name in names:
        if name in allowed:
            continue
        device = ctx.docker("volume", "inspect", "--format", '{{with .Options}}{{index . "device"}}{{end}}', name).strip()
        if overlaps(device):
            raise Error(f"数据目录与另一个卷 {name} 共用或重叠。")
    containers = ctx.docker("ps", "-aq").splitlines()
    if containers:
        sources = ctx.docker("inspect", "--format", '{{range .Mounts}}{{if eq .Type "bind"}}{{println .Source}}{{end}}{{end}}', *containers)
        if any(overlaps(device) for device in sources.splitlines()):
            raise Error("其他容器已直接挂载此数据目录或与其重叠的路径。")


def _prove_host_path(ctx, root, php_tag):
    if not ctx.engine["endpoint"].startswith("unix://"):
        raise Error("无法证明远程 Docker 引擎与本地绑定目录是同一文件系统。")
    parent = Path(root)
    while not parent.exists():
        parent = parent.parent
    # Docker only receives a private, one-use proof file; no host directory is exposed.
    with tempfile.NamedTemporaryFile(mode="w", dir=parent, prefix=".zentaoctl-probe-") as proof:
        nonce = secrets.token_hex(24)
        proof.write(nonce)
        proof.flush()
        # This contains only a disposable nonce; allow the capability-free helper
        # to read it across ordinary and rootless UID mappings.
        os.fchmod(proof.fileno(), 0o444)
        if "," in proof.name:
            raise Error("绑定目录中的逗号不受 Docker --mount 支持。")
        ctx.docker("run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                   "--pull", "never", "--mount", f"type=bind,src={proof.name},dst=/proof,readonly",
                   "--entrypoint", "sh", php_tag, "-ec",
                   'test "$(cat /proof)" = "$1"', "check", nonce)


def _owners(ctx):
    return ctx.docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={ctx.project}").splitlines()


def _print_install(ctx, url, stage):
    if stage == "install":
        print(f"安装向导 {url}/install.php\n数据库主机 mysql，端口 3306。数据库名和用户默认 zentao；口令见 {ctx.env_file} 的 MYSQL_PASSWORD。")
    else:
        print(f"已安装站点 {url}/")
    print(f"维护实例 --project {ctx.project} --env-file {ctx.env_file}")


def _images(ctx, files):
    svc = ctx.model(files)["services"]
    return {s: ctx.docker("image", "inspect", "--format", "{{.Id}}", svc[s]["image"]).strip()
            for s in ("php", "nginx")}


def _volume_marker(ctx, volumes, token):
    path = ctx.state_dir / f"volumes-{token}.yaml"
    labels = {"io.zentaoctl.install": token}
    atomic_write(path, json.dumps({"volumes": {key: {"labels": labels} for key in volumes}}) + "\n")
    return path


def _volume_ids(ctx, names):
    return {name: ctx.docker("volume", "inspect", "--format", "{{.CreatedAt}}", name).strip()
            for name in names}


def install(ctx, args):
    op = dict(ctx.intent or {})
    if op and op.get("command") != "install":
        raise Error("请先继续此前未完成的操作。")
    if ctx.record and not op:
        mode = ctx.record["evidence"].get("targets", {}).get("php")
        if mode == "php-hardened":
            raise Error("此实例已运行加固站点，使用 status 或 recover。")
        files, storage, cache, data, port, _ = _layout(ctx, args)
        report = ctx.require_site()
        if args.port is not None and int(ctx.model()["services"]["nginx"]["ports"][0]["published"]) != port:
            raise Error("已有实例与要求的端口不一致；请用 configure。")
        version = installed_version(ctx)
        stage = "site" if version else "install"
        if not args.dry_run and stage != ctx.record["stage"]:
            ctx.verify(stage)
            ctx.save(report, stage=stage)
        _print_install(ctx, ctx.url(), stage)
        return
    if not op and _owners(ctx):
        raise Error("同名项目已有容器，请先 status/adopt；安装不会接管原站。")

    if op and op["phase"] == "env":
        args = copy.copy(args)
        for attr, key in (("storage", "storage"), ("cache", "cache"), ("data_dir", "data"), ("port", "port")):
            if getattr(args, attr) is None:
                setattr(args, attr, op[key] or None)
    files, storage, cache, data, port, updates = _layout(ctx, args)
    # The intent keeps the original input files across installation/retry.
    if op:
        if (storage, cache, data, port) != (op["storage"], op["cache"], op["data"], op["port"]):
            raise Error("此次参数与已登记的新装意图不符，请沿用原实例、存储和端口。")
        files = [Path(f) for f in op["base_files"]]
    with preview_env(ctx, render_env(ctx.env_file, updates)) as view:
        model = view.model(files)
        volumes = _check_scope(view, model, storage, data, cache)
        _check_ports(ctx, model)
        _check_volume_owner(ctx, model, volumes, op.get("token"))
        if storage == "bind":
            if not ctx.engine["endpoint"].startswith("unix://"):
                raise Error("绑定安装需要可验证的本机 Docker 端点。")
            _check_foreign_devices(ctx, Path(data), set(volumes.values()))
            check_bind_storage(data, resumed=bool(op.get("identity")), identity=op.get("identity"))
        if args.dry_run:
            print(f"将使用普通 PHP/Nginx 安装或继续 {ctx.project}；缓存 {cache}，存储 {storage}。")
            print("目标卷 " + ", ".join(volumes.values()))
            if storage == "bind":
                print(f"执行时将通过只读文件验证 Docker 主机映射 {data}。")
            print(f"安装入口 {view.url(files)}/install.php")
            return

    if not op:
        op = {"command": "install", "phase": "env", "token": secrets.token_hex(16),
              "base_files": list(map(str, files)), "storage": storage, "cache": cache,
              "data": data, "port": port}
        ctx.set_intent(op)
    if op["phase"] == "env":
        write_env(ctx.env_file, updates)
        op["phase"] = "build"
        ctx.set_intent(op)
    if op["phase"] == "build":
        # A build failure occurs before any MySQL, attachment or config volume is created.
        if _owners(ctx):
            raise Error("构建期间发现未登记容器，停止新装。")
        files = files + [_volume_marker(ctx, volumes, op["token"])]
        files = _build(ctx, files, "normal", cache, op["token"])
        key = ctx.state_dir / "config.key"
        if not key.exists():
            atomic_write(key, secrets.token_hex(32))
        op.update(phase="prepare", files=list(map(str, files)), images=_images(ctx, files),
                  signature=ctx.signature(files))
        ctx.set_intent(op)
    files = list(map(Path, op["files"]))
    if ctx.signature(files) != op["signature"] or _images(ctx, files) != op["images"]:
        raise Error("安装候选镜像或生效配置已发生变化，请先恢复此次安装使用的输入。")
    if op["phase"] == "prepare":
        if _owners(ctx):
            raise Error("安装准备阶段出现未登记容器。")
        if storage == "bind":
            _prove_host_path(ctx, data, ctx.model(files)["services"]["php"]["image"])
            op["identity"] = prepare_bind_storage(data)
        op["phase"] = "probe" if storage == "bind" else "create"
        ctx.set_intent(op)
    if op["phase"] == "probe":
        from .fresh_storage import prove_bind_volumes
        php_image = ctx.model(files)["services"]["php"]["image"]
        op["volume_roots"] = prove_bind_volumes(ctx, ctx.model(files), volumes, data, php_image, op["token"])
        op["phase"] = "create"
        ctx.set_intent(op)
    if op["phase"] == "create":
        # 'create' provisions stopped containers and our labelled volumes. No database
        # process runs before the exact volume set is durably recorded.
        if _owners(ctx):
            for cid in _owners(ctx):
                label = ctx.docker("inspect", "--format", '{{index .Config.Labels "com.docker.compose.project.config_files"}}', cid).strip()
                if label.split(",") != list(map(str, files)):
                    raise Error("存在并非此次安装创建的同名容器。")
        ctx.dc("create", "--no-build", files=files)
        _check_volume_owner(ctx, model, volumes, op["token"])
        op.update(phase="start", volume_ids=_volume_ids(ctx, volumes.values()))
        ctx.set_intent(op)
    # After create completed, all expected containers must belong to this run.
    # A missing stopped container can be reconstructed from the verified data.
    report = ctx.snapshot(files=files)
    require_clean(report, allow_missing=True)
    if _volume_ids(ctx, volumes.values()) != op["volume_ids"]:
        raise Error("安装卷缺失或已被替换。请保留现场并恢复原卷。")
    if storage == "bind":
        check_bind_storage(data, resumed=True, identity=op["identity"])
        # Recheck after Compose copy-up, from the freshly created service mounts;
        # a completed restart can no longer rely on an old container's source inode.
        for name, expected_inode in op["volume_roots"].items():
            result = ctx.docker("run", "--rm", "--pull", "never", "--network", "none",
                                "--read-only", "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE",
                                "--mount", f"type=volume,src={name},dst=/proof,readonly,volume-nocopy",
                                "--entrypoint", "stat", op["images"]["php"], "-c", "%i", "/proof").strip()
            if result != str(expected_inode):
                raise Error(f"卷 {name} 已改变，停止启动 MySQL。")
    write_env(ctx.env_file, {"COMPOSE_FILE": _path_list(ctx, files)})
    ctx.dc("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "180", files=files)
    version = installed_version(ctx, files)
    stage = "site" if version else "install"
    ctx.verify(stage, files)
    ctx.save(ctx.snapshot(files=files), stage=stage, files=files)
    ctx.set_intent(None)
    _print_install(ctx, ctx.url(files), stage)


def _original_volumes(ctx, evidence):
    mounts = [m for entries in evidence["mounts"].values() for m in entries.values()
              if m.get("type") == "volume"]
    for mount in mounts:
        name = mount["name"]
        actual = ctx.docker("volume", "inspect", "--format", "{{.CreatedAt}}", name).strip()
        if mount.get("created") != actual:
            raise Error(f"原卷 {name} 的身份发生变化。请保留现场。")


def _harden_identity(ctx, files, op):
    """During an interrupted switch, only the recorded or exact candidate mounts may exist."""
    from .witness import CHECKS, MYSQL, SERVER_ID, STAT, verify_identity
    report = ctx.snapshot(files=files)
    previous = ctx.record["evidence"]
    current = report["evidence"]
    model = ctx.model(files)
    backup = "/var/lib/zentao/backup"
    # A pre-existing backup must belong to this project and match the actual driver options.
    location = next((m for m in model["services"]["php"].get("volumes", []) if m["target"] == backup), None)
    if not location or location["type"] != "volume":
        raise Error("加固配置缺少独立的应用备份卷。")
    volume = model["volumes"][location["source"]]
    name = volume["name"]
    if name in ctx.docker("volume", "ls", "-q").splitlines():
        raw = json.loads(ctx.docker("volume", "inspect", "--format", "{{json .}}", name))
        labels = raw.get("Labels") or {}
        if (labels.get("com.docker.compose.project") != ctx.project or
                labels.get("com.docker.compose.volume") != location["source"] or
                (raw.get("Options") or {}) != (volume.get("driver_opts") or {}) or
                raw.get("Driver") != volume.get("driver", "local")):
            raise Error("现有备份卷的归属、驱动或绑定位置与候选不同。")
        users = ctx.docker("ps", "-a", "--filter", f"volume={name}", "--format",
                           '{{.Label "com.docker.compose.project"}}/{{.Label "com.docker.compose.service"}}').splitlines()
        if any(user != ctx.project + "/php" for user in users):
            raise Error("备份卷还被其他容器使用。")
    for service, mounts in previous["mounts"].items():
        expected = current["mounts"].get(service, {})
        if set(expected) - set(mounts) - ({backup} if service == "php" else set()):
            raise Error(f"{service} 候选出现未登记的额外业务挂载。")
        for dest, old in mounts.items():
            new = dict(expected.get(dest, {}))
            if service == "php" and dest == "/var/lib/zentao/config":
                new["read_only"] = old["read_only"]
            if new != old:
                raise Error(f"{service} 的卷或底层设备发生变化：{dest}")
    allowed_files = [ctx.record["files"], list(map(str, files))]
    for service, info in report["services"].items():
        if (service not in model["services"] or info.get("files") not in allowed_files or
                Path(info["directory"]).resolve() != ctx.root or
                info["image_id"] not in (previous["images"].get(service), op["images"].get(service))):
            raise Error(f"{service} 的镜像、项目路径或 Compose 文件不属于此次切换。")
        live = info["mounts"]
        old = previous["mounts"].get(service, {})
        unexpected = set(live) - set(old) - ({backup} if service == "php" else set())
        # The pinned Redis image declares VOLUME /data even when persistence
        # is disabled and Compose contains no Redis volume definition.
        redis = model["services"][service]
        if service == "redis" and unexpected == {"/data"} and not redis.get("volumes"):
            mount = live["/data"]
            if mount["type"] == "volume" and re.fullmatch(r"[a-f0-9]{64}", mount["name"]):
                volume = json.loads(ctx.docker("volume", "inspect", "--format", "{{json .}}", mount["name"]))
                image_volumes = json.loads(ctx.docker("image", "inspect", "--format",
                                                      "{{json .Config.Volumes}}", info["image_id"])) or {}
                if ("/data" in image_volumes and volume["Driver"] == "local" and not volume.get("Options")
                        and volume.get("Labels") == {"com.docker.volume.anonymous": ""}):
                    unexpected.remove("/data")
        if unexpected:
            raise Error(f"{service} 出现未知挂载。")
        for dest, mount in old.items():
            entry = live.get(dest, {})
            key = "name" if mount["type"] == "volume" else "source"
            if entry.get("type") != mount["type"] or entry.get(key) != mount[key]:
                raise Error(f"{service} 实际数据来源已改变：{dest}")
        if service == "php" and backup in live and live[backup].get("name") != name:
            raise Error("PHP 实际备份卷不属于此实例。")
    verify_identity(ctx, report)
    # A live bind mount can retain the old inode after an operator replaces its
    # source directory. Check the device a newly created container will actually see.
    for service, destinations in CHECKS.items():
        for dest in destinations:
            mount = current["mounts"][service][dest]
            device = mount.get("device") or (mount.get("source") if mount["type"] == "bind" else None)
            if not device:
                continue
            if "," in device:
                raise Error("宿主目录包含无法验证的 Docker 挂载分隔符。")
            argv = ["run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                    "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE", "--security-opt", "no-new-privileges",
                    "--user", "0", "--mount", f"type=bind,src={device},dst=/witness,readonly",
                    "--entrypoint", "sh", previous["images"]["nginx"], "-ec"]
            command = STAT + ("; " + SERVER_ID if dest == MYSQL else "")
            seen = ctx.docker(*argv, command, "check", "/witness").splitlines()
            wanted = [ctx.record["witnesses"][dest]]
            if dest == MYSQL:
                wanted.append(ctx.record["witnesses"]["mysql_uuid"])
            if seen != wanted:
                raise Error(f"宿主机绑定目录的实际身份与原登记不符：{dest}")
    return report


def _legacy_backups(ctx, report, model, token, dry_run):
    from .dataops import _backup_directory, _copy_archive, _local_db_identity
    import tarfile

    mysql_id, php_id = (report["services"][s]["id"] for s in ("mysql", "php"))
    identity = _local_db_identity(ctx, mysql_id)
    directory = _backup_directory(ctx, mysql_id, identity["prefix"])
    if directory.rstrip("/") != "/var/www/zentao/tmp/backup":
        mounts = report["evidence"]["mounts"]["php"]
        if not any(Path(directory) == Path(m) or Path(m) in Path(directory).parents
                   for m in mounts if m != "/var/lib/zentao/config"):
            raise Error("后台自定义备份目录未单独持久化，请先迁移或挂载该目录。")
        return None
    exists = ctx.docker("exec", php_id, "php", "-r",
                        'echo is_dir($argv[1]) && count(scandir($argv[1])) > 2 ? "yes" : "no";', directory).strip()
    if exists != "yes":
        return None
    dest = ctx.state_dir / ("pre-harden-" + token + ".tar.gz")
    if dry_run:
        print("原 PHP 中有备份，执行时将归档并导入加固备份卷。")
        return dest
    part = dest.with_suffix(".partial-" + secrets.token_hex(6))
    try:
        _copy_archive(ctx, ["exec", php_id, "tar", "-C", directory, "-cf", "-", "."], part)
        with tarfile.open(part, "r:gz") as archive:
            for item in archive:
                p = Path(item.name)
                if p.is_absolute() or ".." in p.parts or not (item.isfile() or item.isdir()):
                    raise Error("旧备份中含非普通文件或链接，请先核实并妥善迁移。")
        os.chmod(part, 0o600)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def _restore_legacy(ctx, files, path):
    with Path(path).open("rb") as archive:
        code = subprocess.run(ctx.compose(files) + ["exec", "-T", "php", "tar", "-C",
                              "/var/lib/zentao/backup", "--skip-old-files", "-xzf", "-"],
                              stdin=archive, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=ctx.environ).returncode
    if code:
        raise Error(f"旧备份已保存到 {path}，导入尚未完成，请重试 harden。")


def harden(ctx, args):
    op = dict(ctx.intent or {})
    if op and op.get("command") != "harden":
        raise Error("先继续此前未完成的操作。")
    if not ctx.record:
        raise Error("请先 adopt 已安装的普通站点，或运行 install。")
    old = list(map(Path, ctx.record["files"]))
    if not op:
        report = ctx.require_site()
        normal = ctx.model(old)
        if normal["services"]["php"]["build"]["target"] == "php-hardened":
            ctx.verify("site", old)
            print("站点已经使用加固镜像。")
            return
        version = installed_version(ctx, old)
        if not version:
            raise Error(f"安装向导尚未完成；请在 {ctx.url(old)}/install.php 完成安装并登录。")
        running = ctx.dc("exec", "-T", "php", "cat", "/var/www/zentao/VERSION", files=old).strip()
        if version != running:
            raise Error(f"数据库版本 {version} 与运行版本 {running} 不一致，请先完成应用升级。")
        files = without_candidates(ctx, old)
        files.append(ctx.root / "compose.hardened.yaml")
        model = ctx.model(files)
        # Only the backup mount is new; all three existing business mounts keep their source.
        for service, before in normal["services"].items():
            after = {m["target"]: m.get("source") for m in model["services"][service].get("volumes", [])}
            for m in before.get("volumes", []):
                if m.get("type") in ("volume", "bind") and m.get("source") != after.get(m["target"]):
                    raise Error(f"加固会改变 {service} 的数据来源，停止切换。")
        _harden_identity(ctx, files, {"images": {}})
        token = secrets.token_hex(16)
        if args.dry_run:
            _legacy_backups(ctx, report, normal, token, True)
            print("将保留原数据卷，构建独立加固 PHP/Nginx，切换全部服务并检查登录入口。")
            return
        op = {"command": "harden", "phase": "build", "token": token, "version": version,
              "base_files": list(map(str, files)), "cache": _cache(normal),
              "archive": None}
        ctx.set_intent(op)
    elif args.dry_run:
        print(f"将继续加固步骤 {op['phase']}，使用此前登记的卷与候选镜像。")
        return
    _original_volumes(ctx, ctx.record["evidence"])
    if op["phase"] == "build":
        ctx.require_site()
        files = _build(ctx, list(map(Path, op["base_files"])), "hardened", op["cache"], op["token"])
        candidate_version = ctx.docker("run", "--rm", "--network", "none", "--read-only",
                                       "--pull", "never", "--entrypoint", "cat",
                                       ctx.model(files)["services"]["php"]["image"],
                                       "/var/www/zentao/VERSION").strip()
        if candidate_version != op["version"]:
            raise Error("加固候选的应用版本与已装数据库不符，请使用正式升级流程。")
        ctx.require_site()
        op.update(phase="switch", files=list(map(str, files)), signature=ctx.signature(files),
                  images=_images(ctx, files))
        ctx.set_intent(op)
    files = list(map(Path, op["files"]))
    if ctx.signature(files) != op["signature"] or _images(ctx, files) != op["images"]:
        raise Error("待切换的配置或镜像已改变，请恢复原候选再重试。")
    if ctx.signature(ctx.files()) not in (op["signature"], ctx.record["signature"]):
        raise Error("切换期间原环境配置发生变化，停止重建。")
    report = _harden_identity(ctx, files, op)
    if op["phase"] == "switch":
        # On retry an old PHP may still have produced backups since the last attempt.
        php = report["services"].get("php", {})
        if php.get("image_id") == ctx.record["evidence"]["images"]["php"] and php.get("state") == "running":
            archive = _legacy_backups(ctx, report, ctx.model(old), op["token"], False)
            if archive:
                op["archive"] = str(archive)
                ctx.set_intent(op)
        write_env(ctx.env_file, {"COMPOSE_FILE": _path_list(ctx, files)})
        ctx.dc("up", "-d", "--force-recreate", "--no-build", "--pull", "never",
               "--wait", "--wait-timeout", "180", files=files)
        op["phase"] = "verify"
        ctx.set_intent(op)
    if op.get("archive"):
        _restore_legacy(ctx, files, op["archive"])
    ctx.verify("site", files)
    ctx.dc("exec", "-T", "php", "sh", "-ec",
           'test "$(id -u)" = 33 && test ! -e /var/www/zentao/www/install.php && test ! -e /var/www/zentao/www/upgrade.php',
           files=files)
    if installed_version(ctx, files) != op["version"]:
        raise Error("加固后未确认原站数据库版本，请核对挂载与版本。")
    ctx.save(ctx.snapshot(files=files), stage="site", files=files)
    ctx.set_intent(None)
    print(f"原站已切换为加固模式 {ctx.url(files)}/")
    if op.get("archive"):
        print(f"旧备份归档已保留在 {op['archive']}。")
