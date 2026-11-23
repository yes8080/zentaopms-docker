#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "用法: $0 http://127.0.0.1:8080" >&2
    exit 2
fi
case "$1" in
    http://?*|https://?*) ;;
    *) echo "请提供站点的 HTTP 或 HTTPS 地址。" >&2; exit 2 ;;
esac

services=$(docker compose config --services)
for service in mysql redis php nginx; do
    if ! printf '%s\n' "$services" | grep -Fxq "$service"; then
        echo "当前 Compose 配置缺少 $service。请核对部署目录、项目名和 .env 中的 COMPOSE_FILE。" >&2
        exit 1
    fi
done

docker compose up -d --wait --wait-timeout 180
"$(dirname "$0")/check-deployment.sh" "$1" site
