#!/bin/sh
set -eu

if [ "${1:-}" = "--help" ]; then
    echo "用法: $0 [Compose 全局参数]。请传入部署时相同的 -p、-f 和 --env-file。"
    echo "只读比较 MySQL、PHP、Nginx 和可选 Redis 的 Docker 内存/PID 配置与内核 cgroup 实值。"
    exit 0
fi

status=0
driver=$(docker info --format '{{.CgroupDriver}}')
case "$driver" in
    none|'')
        echo "Docker 守护进程没有 cgroup 驱动，无法按容器执行资源限制。" >&2
        status=1 ;;
esac

# Read PID 1's cgroup, including when a container uses the host cgroup namespace.
read_limits() {
    docker exec -i "$1" sh -s <<'CGROUP'
set -eu
group_directory() {
    mountpoint=$1
    group=$2
    test -n "$group"
    mount_root=$(awk -v path="$mountpoint" '$5 == path {print $4; exit}' /proc/1/mountinfo)
    test -n "$mount_root"
    case "$group" in
        "$mount_root") printf '%s\n' "$mountpoint" ;;
        "${mount_root%/}"/*) printf '%s%s\n' "$mountpoint" "${group#"${mount_root%/}"}" ;;
        *) return 1 ;;
    esac
}
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    relative=$(awk -F: '$1 == "0" && $2 == "" {print $3; exit}' /proc/1/cgroup)
    base=$(group_directory /sys/fs/cgroup "$relative")
    cat "$base/memory.max" "$base/pids.max"
else
    for controller in memory pids; do
        relative=$(awk -F: -v c="$controller" 'index("," $2 ",", "," c ",") {print $3; exit}' /proc/1/cgroup)
        base=$(group_directory "/sys/fs/cgroup/$controller" "$relative")
        case "$controller" in
            memory) cat "$base/memory.limit_in_bytes" ;;
            pids)   cat "$base/pids.max" ;;
        esac
    done
fi
CGROUP
}

services=$(docker compose "$@" config --services)
for service in mysql php nginx redis; do
    if [ "$service" = redis ] && ! printf '%s\n' "$services" | grep -qx redis; then continue; fi
    containers=$(docker compose "$@" ps --all --quiet "$service")
    if [ -z "$containers" ]; then
        echo "$service 未找到已部署的容器。" >&2
        status=1
        continue
    fi
    for container in $containers; do
        expected=$(docker inspect --format '{{.HostConfig.Memory}} {{.HostConfig.PidsLimit}}' "$container")
        if ! actual=$(read_limits "$container"); then
            echo "$service ($container) 无法读取进程的内存和 PID 限额。" >&2
            status=1
            continue
        fi
        memory=${expected% *}
        pids=${expected#* }
        actual_memory=$(printf '%s\n' "$actual" | sed -n '1p')
        actual_pids=$(printf '%s\n' "$actual" | sed -n '2p')
        if [ "$memory" -gt 0 ] && [ "$pids" -gt 0 ] 2>/dev/null \
            && [ "$memory" = "$actual_memory" ] && [ "$pids" = "$actual_pids" ]; then
            echo "$service ($container) 内存上限 $memory 字节，PID 上限 $pids，内核实值一致。"
        else
            echo "$service ($container) 内存字节/PID 期望 $memory/$pids，内核实值 $actual_memory/$actual_pids。" >&2
            status=1
        fi
    done
done
exit "$status"
