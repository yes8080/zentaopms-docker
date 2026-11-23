"""Local deployment records and explicit Compose invocations."""
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import tempfile

from . import site


class Error(RuntimeError):
    pass


def atomic_write(path, text, mode=0o600):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & ~mode:
                raise Error(f"文件系统无法保护 {path} 的私有权限，请选择 Linux 文件系统上的目录。")
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path):
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise Error(f"登记文件格式异常，请检查 {path}")
    return data


def valid_project(name):
    return re.fullmatch("[a-z0-9][a-z0-9_-]*", name or "") is not None


def run(argv, env=None, cwd=None, stdin=None, label="Docker"):
    result = subprocess.run(argv, input=stdin, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=cwd)
    if result.returncode:
        # Docker/Compose may echo resolved credentials and syntax errors in .env.
        raise Error(f"{label} 失败（退出码 {result.returncode}）。请核对 Docker 端点、环境文件、覆盖文件及服务状态。")
    return result.stdout


def clean_environment():
    # Site interpolation comes from the selected env file. Keep Docker transport,
    # credential helpers and build/proxy settings for the underlying client.
    allowed = {"PATH", "HOME", "LANG", "TERM", "TMPDIR", "SSH_AUTH_SOCK", "SYSTEMROOT",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "XDG_RUNTIME_DIR",
               "XDG_CONFIG_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    return {k: v for k, v in os.environ.items() if k.upper() in allowed
            or k.startswith(("DOCKER_", "BUILDX_", "BUILDKIT_", "LC_"))}


@contextlib.contextmanager
def project_lock(engine, project):
    directory = Path("/tmp") / f"zentaoctl-locks-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Error("本机的实例锁目录权限异常。")
    key = hashlib.sha256((engine["id"] + "/" + project).encode()).hexdigest()
    fd = os.open(directory / key, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error(f"项目 {project} 正在执行另一个维护操作。") from None
        yield
    finally:
        os.close(fd)


class Context:
    def __init__(self, root, project, env_file, engine):
        self.root = Path(root).resolve()
        self.project = project
        self.engine = engine
        self.state_dir = self.root / ".zentaoctl" / project
        if self.state_dir.is_symlink() or self.state_dir.parent.is_symlink():
            raise Error("本地登记目录不能是符号链接。")
        self.record = read_json(self.state_dir / "release.json")
        self.intent = read_json(self.state_dir / "operation.json")
        owner = self.record or self.intent or {}
        selected = Path(env_file or owner.get("env_file") or self.root / ".env").expanduser().absolute()
        if selected.is_symlink() or any(parent.is_symlink() for parent in selected.parents):
            raise Error("环境文件路径含符号链接，请使用实际文件的绝对路径。")
        self.env_file = selected.resolve()
        for known in (self.record, self.intent):
            if not known:
                continue
            if (any(known.get("engine", {}).get(k) != engine[k] for k in ("id", "endpoint"))
                    or known.get("project") != project):
                raise Error("当前 Docker 引擎或项目与登记不符，请切回原实例。")
            if known.get("env_file") != str(self.env_file):
                raise Error(f"此项目登记的环境文件为 {known['env_file']}，请核对 --env-file。")
        self.environ = clean_environment()

    def docker(self, *args):
        return run(["docker", *map(str, args)], self.environ, self.root,
                   label=f"docker {args[0]}")

    def settings(self):
        if not self.env_file.is_file():
            raise Error(f"找不到环境文件 {self.env_file}。新站请运行 install。")
        argv = ["docker", "compose", "--project-directory", str(self.root),
                "--env-file", str(self.env_file), "-p", self.project, "-f", "-",
                "config", "--format", "json"]
        # Compose resolves dotenv quoting and multiline values. JSON keeps a line
        # in an unrelated secret from accidentally becoming a control setting.
        keys = ("COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR", "COMPOSE_PROJECT_NAME",
                "MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "ZT_REDIS_PASSWORD",
                "ZENTAO_DATA_DIR", "ZENTAO_BIND", "ZENTAO_PORT")
        probe = {"services": {"settings": {"image": "scratch",
                 "environment": {k: "${" + k + "-}" for k in keys}}}}
        result = run(argv, self.environ, self.root, json.dumps(probe), "解析环境文件")
        resolved = json.loads(result)["services"]["settings"]["environment"]
        return {name: value.replace("$$", "$") for name, value in resolved.items()}

    def files(self):
        settings = self.settings()
        separator = settings.get("COMPOSE_PATH_SEPARATOR") or os.pathsep
        value = settings.get("COMPOSE_FILE")
        if value:
            if not separator or any(not part for part in value.split(separator)):
                raise Error("环境文件中的 COMPOSE_FILE 含空路径。")
            paths = [self.root / p for p in value.split(separator)]
        else:
            paths = [self.root / "compose.yaml"]
            paths.extend(p for p in (self.root / "compose.override.yaml", self.root / "compose.override.yml")
                         if p.is_file())
        files = [p.resolve() for p in paths]
        for path in files:
            if not path.is_file():
                raise Error(f"缺少 Compose 文件 {path}。请恢复原文件及顺序。")
        return files

    def compose(self, files=None):
        argv = ["docker", "compose", "--project-directory", str(self.root),
                "--env-file", str(self.env_file), "-p", self.project]
        for path in self.files() if files is None else files:
            argv.extend(["-f", str(path)])
        return argv

    def dc(self, *args, files=None):
        prefix = self.compose(files)
        command = prefix + list(map(str, args))
        try:
            return run(command, self.environ, self.root, label=f"Compose {args[0]} ({self.project})")
        except Error as exc:
            if args[0] in ("build", "pull"):
                raise Error(f"{exc}\n在部署主机查看原始构建或拉取输出：{shlex.join(command)}") from None
            if args[0] in ("create", "up", "restart", "stop"):
                raise Error(f"{exc}\n查看本实例容器：{shlex.join(prefix + ['ps', '-a'])}") from None
            raise

    def model(self, files=None):
        return json.loads(self.dc("config", "--format", "json", files=files))

    def snapshot(self, previous=None, files=None):
        files = self.files() if files is None else files
        report = site.inspect_site(self.project, self.model(files), files, self.root, previous)
        hashes = dict(line.split() for line in self.dc("config", "--hash", "*", files=files).splitlines())
        for name, service in report["services"].items():
            if name in hashes and service.get("config_hash") != hashes[name]:
                report["blockers"].append(f"{name} 容器的 Compose 配置摘要与当前配置不同，请核实启动文件及 Compose 版本。")
        return report

    def signature(self, files):
        key_path = self.state_dir / "config.key"
        if not key_path.is_file():
            raise Error("缺少本机配置校验密钥，请从本实例的受保护备份恢复。")
        key = bytes.fromhex(key_path.read_text().strip())
        canonical = json.dumps(self.model(files), sort_keys=True, ensure_ascii=False).encode()
        return hmac.new(key, canonical, hashlib.sha256).hexdigest()

    def registered(self):
        if not self.record:
            raise Error(f"请先用 adopt --project {self.project} 核实并登记旧站，或用 install 新建站点。")
        files = self.files()
        if list(map(str, files)) != self.record["files"]:
            raise Error("Compose 文件集与登记不一致。原文件集为 " + ", ".join(self.record["files"]))
        if self.signature(files) != self.record["signature"]:
            raise Error("当前生效配置与验收记录存在差异。请恢复原配置后运行 configure 或 update。")
        return self.snapshot(self.record["evidence"], files)

    def require_site(self):
        report = self.registered()
        require_clean(report)
        if report["unverified"]:
            raise Error("\n".join(report["unverified"]))
        from .witness import verify_identity
        verify_identity(self, report)
        return report

    def set_intent(self, intent):
        path = self.state_dir / "operation.json"
        if intent is None:
            path.unlink(missing_ok=True)
        else:
            intent = dict(intent, project=self.project, engine=self.engine, env_file=str(self.env_file))
            atomic_write(path, json.dumps(intent, ensure_ascii=False, indent=2) + "\n")
        self.intent = intent

    def save(self, report, stage="site", files=None):
        require_clean(report)
        if report["unverified"]:
            raise Error("\n".join(report["unverified"]))
        files = self.files() if files is None else files
        key = self.state_dir / "config.key"
        if not key.exists():
            if self.record:
                raise Error("已有登记缺少配置校验密钥。")
            atomic_write(key, secrets.token_hex(32))
        from .witness import capture
        record = dict(project=self.project, engine=self.engine, files=list(map(str, files)),
                      env_file=str(self.env_file), evidence=report["evidence"], stage=stage,
                      witnesses=capture(self, report, stage), signature=self.signature(files))
        record["commits"] = {}
        labels = [json.loads(self.docker("image", "inspect", "--format", "{{json .Config.Labels}}",
                                         report["evidence"]["images"][svc])) or {}
                  for svc in ("php", "nginx")]
        for repo, field in (("zentaopms", "app-revision"), (self.root.name, "deployment-revision")):
            values = {image.get("io.zentaoctl." + field, "") for image in labels}
            if len(values) == 1 and re.fullmatch(r"[a-f0-9]{40,64}", next(iter(values))):
                record["commits"][repo] = next(iter(values))
        git = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--verify", "HEAD"],
                             capture_output=True, text=True)
        if git.returncode == 0:
            record["compose_revision"] = git.stdout.strip()
        atomic_write(self.state_dir / "release.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        self.record = record

    def url(self, files=None):
        ports = self.model(files)["services"]["nginx"].get("ports", [])
        for mapping in ports:
            if int(mapping["target"]) == 80 and mapping.get("published"):
                host = mapping.get("host_ip") or "127.0.0.1"
                host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
                if ":" in host:
                    host = f"[{host}]"
                return f"http://{host}:{int(mapping['published'])}"
        raise Error("找不到 Nginx 的 HTTP 发布端口。")

    def verify(self, stage="site", files=None):
        files = self.files() if files is None else files
        url = self.url(files)
        if not self.engine.get("endpoint", "").startswith("unix://"):
            raise Error("宿主 HTTP 验收需要本地 Docker 端点。请从部署主机执行。")
        arguments = [str(self.root / "scripts/check-deployment.sh"), url,
                     "install" if stage in ("install", "installer") else "site"]
        print(run(arguments, self.environ, self.root, label="禅道 HTTP 检查").strip())
        model = self.model(files)
        self.dc("exec", "-T", "php", "php", "-r", DB_PROBE, files=files)
        if "redis" in model["services"]:
            self.dc("exec", "-T", "php", "php", "-r",
                    '$r=new Redis(); if (!$r->connect("redis",6379,3) || '
                    '!$r->auth(getenv("ZT_REDIS_PASSWORD")) || !$r->ping()) exit(1);', files=files)
        elif model["services"]["php"].get("environment", {}).get("ZT_CACHE_DRIVER") == "apcu":
            self.dc("exec", "-T", "php", "php", "-r",
                    'exit(extension_loaded("apcu") ? 0 : 1);', files=files)
        if stage not in ("install", "installer"):
            from .install import database_identity, installed_version
            identity = database_identity(self, files)
            mysql = model["services"]["mysql"]
            configured_db = (mysql.get("environment") or {}).get("MYSQL_DATABASE")
            if configured_db:
                configured_db = configured_db.replace("$$", "$")
            local_uuid = self.dc("exec", "-T", "--user", "0", "mysql", "awk", "-F=",
                                 "/^server-uuid=/{print $2}", "/var/lib/mysql/auto.cnf", files=files).strip()
            if identity["uuid"] != local_uuid or (configured_db and identity["name"] != configured_db):
                raise Error("禅道连接的数据库与此项目的 MySQL 实例或数据库名不一致，请核对原站配置。")
            if not installed_version(self, files):
                raise Error("数据库中未找到禅道安装版本，不能认定原站恢复。")
        print("PHP 依赖连通检查通过。网页登录、原业务记录与历史附件需在浏览器核对。")


DB_PROBE = '$s=@fsockopen(getenv("MYSQL_HOST") ?: "mysql",(int)(getenv("MYSQL_PORT") ?: 3306),$e,$m,3); exit($s ? 0 : 1);'


def require_clean(report, allow_missing=False):
    errors = list(report["blockers"])
    if not allow_missing and report["missing"]:
        errors.append("缺少容器 " + ", ".join(report["missing"]))
    if errors:
        raise Error("\n".join(errors))
