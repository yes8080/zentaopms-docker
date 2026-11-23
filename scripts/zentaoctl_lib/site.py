"""Read-only Docker observations for a Compose site. Never print raw inspect output."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


LABEL = "com.docker.compose."
DATA_PATHS = {
    "mysql": {"/var/lib/mysql"},
    "php": {"/var/lib/zentao/config", "/var/www/zentao/www/data"},
    "nginx": {"/var/www/zentao/www/data"},
}
BACKUP = "/var/lib/zentao/backup"


def _docker(*args, optional=False):
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError("Docker 客户端不可用") from exc
    if result.returncode:
        if optional and re.search(r"no such (?:image|volume|object)|not found", result.stderr, re.I):
            return None
        raise RuntimeError(f"Docker 只读检查失败 ({args[0]})，请检查端点、权限和服务状态。")
    return result.stdout.strip()


def engine_identity():
    context = _docker("context", "show")
    endpoint = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
    if not endpoint:
        endpoint = _docker("context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}")
    engine_id = _docker("info", "--format", "{{.ID}}")
    if not endpoint or not engine_id:
        raise RuntimeError("无法确定 Docker 引擎身份。")
    # SSH/TCP endpoints may contain credentials. Preserve the identity without disclosing them.
    if "://" in endpoint and "@" in endpoint.split("://", 1)[1]:
        endpoint = endpoint.split("://", 1)[0] + "://sha256:" + hashlib.sha256(endpoint.encode()).hexdigest()
    return {"context": context, "endpoint": endpoint, "id": engine_id}


def discover():
    template = '{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}\t{{.Label "com.docker.compose.oneoff"}}'
    rows = _docker("ps", "-a", "--filter", "label=" + LABEL + "project", "--format", template)
    result = {}
    for row in rows.splitlines():
        fields = row.split("\t")
        if len(fields) >= 2 and fields[0] and fields[1] and (len(fields) < 3 or fields[2].lower() != "true"):
            result.setdefault(fields[0], set()).add(fields[1])
    return {project: sorted(services) for project, services in sorted(result.items())}


def projects_in_directory(directory):
    rows = _docker("ps", "-a", "--filter", "label=" + LABEL + "project",
                   "--format", '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}')
    root = Path(directory).resolve()
    return {name for row in rows.splitlines() for name, cwd in [row.split("|", 1)]
            if cwd and Path(cwd).resolve() == root}


def _inspect(kind, reference):
    value = _docker(kind, "inspect", "--", reference, optional=True)
    return json.loads(value)[0] if value else None


def _target(image, history):
    # Distinguish the final application configuration, which can share a Compose tag.
    entry = image["Config"].get("Entrypoint") or []
    if entry == ["zentao-hardened-entrypoint"] and "harden-router.php" in history:
        return "php-hardened"
    if entry == ["docker-php-entrypoint"] and "php.ini /usr/local/etc/php/conf.d/zentao.ini" in history:
        return "php"
    for line in history.splitlines():   # image history is ordered newest first
        if line.startswith("COPY ") and " /etc/nginx/conf.d/default.conf " in line:
            match = re.search(r"(?:^|/| )(nginx(?:\.hardened)?\.conf) /etc/nginx/conf.d/default.conf ", line)
            if match:
                return "nginx-hardened" if match[1] == "nginx.hardened.conf" else "nginx"
            break
    return None


def _volume_details(spec, definitions, volumes, blockers, where, project):
    source = spec.get("source", "")
    kind = spec["type"]
    detail = {"type": kind, "name": "", "source": "", "read_only": bool(spec.get("read_only")),
              "driver": "", "device": "", "created": ""}
    if kind == "bind":
        detail["source"] = source
        return detail
    if kind != "volume" or not source:
        blockers.append(f"{where} 缺少可识别的持久化挂载。")
        return detail
    definition = definitions.get(source, {})
    name = definition.get("name", source)
    detail["name"] = name
    if name not in volumes:
        volumes[name] = _inspect("volume", name)
    volume = volumes[name]
    if not volume:
        blockers.append(f"{where} 需要的卷 {name} 不存在。")
        return detail
    options = volume.get("Options") or {}
    wanted = definition.get("driver_opts") or {}
    external = definition.get("external")
    labels = volume.get("Labels") or {}
    if not external and (labels.get(LABEL + "project") != project or labels.get(LABEL + "volume") != source):
        blockers.append(f"{where} 卷 {name} 的项目或逻辑卷标签不匹配，请核实原卷归属。")
    if not external:
        if (definition.get("driver", "local") != volume.get("Driver") or
                {k: str(v) for k, v in wanted.items()} != options):
            blockers.append(f"{where} 卷 {name} 的驱动或绑定目录与配置不同。")
    detail.update(driver=volume["Driver"], created=volume.get("CreatedAt") or "")
    if volume["Driver"] == "local" and {"bind", "rbind"}.intersection(options.get("o", "").split(",")):
        detail["device"] = detail["source"] = options.get("device", "")
    return detail


def _containers(project):
    ids = _docker("ps", "-a", "-q", "--no-trunc", "--filter", f"label={LABEL}project={project}").split()
    if not ids:
        return []
    return json.loads(_docker("container", "inspect", "--", *ids))


def _file(path):
    return str(Path(path).resolve())


def inspect_site(project, config, files, directory, previous=None):
    """Inspect secrets in memory only. Accept previous as a previously returned evidence dict."""
    blockers, unverified, missing = [], [], []
    services, volumes, images, targets, mounts = {}, {}, {}, {}, {}
    known_files = [_file(p) for p in files]
    desired = config.get("services", {})
    if config.get("name") != project:
        blockers.append("Compose 解析的项目名与目标项目不一致。")
    if not {"mysql", "php", "nginx"}.issubset(desired):
        blockers.append("当前 Compose 配置缺少 MySQL、PHP 或 Nginx 服务。")
    actual = {}
    for container in _containers(project):
        labels = container["Config"].get("Labels") or {}
        if labels.get(LABEL + "oneoff", "").lower() == "true":
            continue
        name = labels.get(LABEL + "service")
        if not name or labels.get(LABEL + "project") != project:
            blockers.append("容器缺少明确的服务或项目归属。")
            continue
        if name in actual:
            blockers.append(f"{name} 包含多个容器，无法唯一确定服务身份。")
        actual[name] = container
        files_label = labels.get(LABEL + "project.config_files", "")
        paths = files_label.split(",") if files_label else []
        cwd = labels.get(LABEL + "project.working_dir", "")
        state = container["State"]
        services[name] = {
            "id": container["Id"], "state": state["Status"],
            "health": state.get("Health", {}).get("Status", ""),
            "image_id": container["Image"], "files": paths, "directory": cwd,
            "user": container["Config"].get("User", ""),
            "read_only": bool(container["HostConfig"].get("ReadonlyRootfs")),
            "mounts": {m["Destination"]: {
                "type": m["Type"], "name": m.get("Name", ""),
                "source": m.get("Source", "") if m["Type"] == "bind" else "",
                "read_only": not bool(m["RW"])}
                for m in container["Mounts"] if m["Type"] in {"bind", "volume"}},
            "env_file": labels.get(LABEL + "project.environment_file", ""),
            "config_hash": labels.get(LABEL + "config-hash", ""),
        }
        if [_file(p) for p in paths] != known_files or not cwd or _file(cwd) != _file(directory):
            blockers.append(f"{name} 的历史 Compose 文件清单或工作目录与本次配置不符。")
        if name not in desired:
            blockers.append(f"当前配置遗漏已有服务 {name}。")
    for name, cfg in sorted(desired.items()):
        container = actual.get(name)
        if not container:
            missing.append(name)
        live_mounts = {m["Destination"]: m for m in container["Mounts"]} if container else {}
        specs = {m["target"]: m for m in cfg.get("volumes", []) if isinstance(m, dict)}
        protected = DATA_PATHS.get(name, set())
        if name == "php" and BACKUP in live_mounts:
            protected = protected | {BACKUP}
        for target in sorted(protected - specs.keys()):
            blockers.append(f"{name}:{target} 缺少原数据挂载。")
        mounts[name] = {}
        for target, spec in sorted(specs.items()):
            if spec["type"] not in {"bind", "volume"} and target not in protected:
                continue
            where = f"{name}:{target}"
            detail = _volume_details(spec, config.get("volumes", {}), volumes, blockers, where, project)
            mounts[name][target] = detail
            if container:
                live = live_mounts.get(target)
                if (not live or live["Type"] != detail["type"] or
                        (live.get("Name", "") if live["Type"] == "volume" else live["Source"]) !=
                        (detail["name"] if detail["type"] == "volume" else detail["source"]) or
                        bool(live["RW"]) == detail["read_only"]):
                    old = (live.get("Name") or live.get("Source") or live["Type"]) if live else "无挂载"
                    new = detail["name"] or detail["source"] or detail["type"]
                    blockers.append(f"{where}：现场 {old}，计划 {new}，挂载来源或读写方式不一致。")
        if container:
            expected_env = cfg.get("environment") or {}
            runtime_env = dict(pair.split("=", 1) for pair in (container["Config"].get("Env") or []) if "=" in pair)
            if any(runtime_env.get(k) != (str(v).replace("$$", "$") if v is not None else None) for k, v in expected_env.items()):
                blockers.append(f"{name} 的容器环境与本次 Compose 配置不一致（值已隐藏）。")
            expected_ports = set()
            for port in cfg.get("ports", []):
                published = str(port.get("published") or "")
                expected_ports.add((f'{port["target"]}/{port.get("protocol", "tcp")}', port.get("host_ip") or "", published))
            actual_ports = {(key, binding.get("HostIp") or "", binding.get("HostPort") or "")
                            for key, bindings in (container["HostConfig"].get("PortBindings") or {}).items()
                            for binding in (bindings or [])}
            if actual_ports != expected_ports:
                blockers.append(f"{name} 的发布地址或端口与本次配置不同。")
            if bool(cfg.get("read_only")) != services[name]["read_only"] or (cfg.get("user") and
                    str(cfg["user"]) != services[name]["user"]):
                blockers.append(f"{name} 的运行用户或根文件系统权限与配置不符。")
            networks = {config.get("networks", {}).get(n, {}).get("name", n)
                        for n in (cfg.get("networks") or {})}
            if networks and networks != set(container["NetworkSettings"]["Networks"]):
                blockers.append(f"{name} 的网络与配置不一致。")
        ref = cfg.get("image") or f"{project}-{name}"
        image = _inspect("image", ref)
        if not image:
            blockers.append(f"{name} 的计划镜像未在当前引擎找到。")
            continue
        image_id = image["Id"]
        images[name] = image_id
        if container and image_id != container["Image"]:
            blockers.append(f"{name} 的镜像标签与当前容器的镜像 ID 不一致。")
        wanted_target = (cfg.get("build") or {}).get("target")
        if wanted_target in {"nginx", "nginx-hardened", "php", "php-hardened"}:
            history = _docker("image", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image_id)
            target = _target(image, history)
            # Managed builds have an independent candidate tag and explicit build
            # labels. Legacy builds still use their image history as evidence.
            declared = (cfg["build"].get("labels") or {})
            managed = "io.zentaoctl.candidate" in declared
            if managed:
                actual_labels = image["Config"].get("Labels") or {}
                keys = ("mode", "service", "candidate", "cache")
                if any(not declared.get("io.zentaoctl." + key) or
                       actual_labels.get("io.zentaoctl." + key) != declared["io.zentaoctl." + key]
                       for key in keys):
                    blockers.append(f"{name} 镜像标记与登记的候选构建不同。")
                else:
                    claimed = name + ("-hardened" if declared["io.zentaoctl.mode"] == "hardened" else "")
                    if target and target != claimed:
                        blockers.append(f"{name} 的镜像构建标记与镜像层历史冲突。")
                    target = target or claimed
            targets[name] = target or ""
            if name == "php" and target:
                expected_cache = str((cfg["build"].get("args") or {}).get("ZENTAO_CACHE_EXTENSION", "none"))
                if managed:
                    built_cache = {declared.get("io.zentaoctl.cache")}
                else:
                    built_cache = set(re.findall(r"^RUN [^\n]*? ZENTAO_CACHE_EXTENSION=(redis|apcu|none) ", history, re.M))
                if not built_cache:
                    unverified.append("PHP 镜像缺少缓存扩展构建证据。")
                elif built_cache != {expected_cache}:
                    blockers.append("PHP 镜像的缓存扩展与 Compose 构建参数不符。")
            if not target:
                unverified.append(f"{name} 的旧镜像缺少可识别的构建目标证据。")
            elif wanted_target != target:
                blockers.append(f"{name} 计划使用 {wanted_target}，本地镜像指纹为 {target}。")
    app_path = "/var/www/zentao/www/data"
    php_data = mounts.get("php", {}).get(app_path)
    nginx_data = mounts.get("nginx", {}).get(app_path)
    if php_data and nginx_data and (not nginx_data["read_only"] or php_data["read_only"] or
            any(php_data[k] != nginx_data[k] for k in ("type", "name", "source"))):
        blockers.append("PHP 与 Nginx 必须引用同一附件数据，PHP 可写且 Nginx 只读。")
    retained = _docker("volume", "ls", "-q", "--filter", f"label={LABEL}project={project}").splitlines()
    attached = {m.get("Name") for c in actual.values() for m in c["Mounts"]}
    inactive = sorted(set(retained) - attached - set(volumes))
    evidence = {"mounts": mounts, "images": images, "targets": targets}
    if previous is not None:
        for key, current in evidence.items():
            old = previous.get(key) or {}
            for name in sorted(set(old) | set(current)):
                if old.get(name) != current.get(name):
                    blockers.append(f"{name} 的 {key} 与上次登记不一致。")
    return {"services": services, "evidence": evidence, "missing": missing,
            "blockers": blockers, "unverified": unverified, "extra_volumes": inactive}
