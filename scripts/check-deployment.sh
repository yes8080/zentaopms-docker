#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo "用法: $0 http://127.0.0.1:8080 install|site" >&2
    exit 2
fi

base_url=${1%/}
case "$base_url" in
    http://*|https://*) ;;
    *) echo "请提供带 http:// 或 https:// 的站点地址。" >&2; exit 2 ;;
esac
case "$2" in
    install) path=/install.php ;;
    site) path=/ ;;
    *) echo "检查模式须为 install 或 site。" >&2; exit 2 ;;
esac

check_http() {
    curl --noproxy '*' --silent --show-error --connect-timeout 3 --max-time 15 \
        --output /dev/null --write-out '%{http_code}\n%{redirect_url}\n' "$1"
}

report_failure() {
    echo "禅道访问检查失败 ($1)。" >&2
    echo "请沿用部署的项目名和全部 Compose 文件，检查 ps -a 和 logs --tail=80 nginx php mysql redis（未启用 Redis 时省略 redis）。" >&2
    echo "如遇连接超时或 502/504，请按 README 的容器网络排查步骤检查。" >&2
    exit 1
}

result=$(check_http "$base_url$path") || report_failure '连接或请求失败'
status=$(printf '%s\n' "$result" | sed -n '1p')
redirect=$(printf '%s\n' "$result" | sed -n '2p')
case "$status:$2" in
    200:*) ;;
    302:site|303:site)
        case "$redirect" in
            "$base_url"/index.php\?m=user\&f=login|"$base_url"/index.php\?m=user\&f=login\&*)
                login_result=$(check_http "$redirect") || report_failure '登录页请求失败'
                login_status=$(printf '%s\n' "$login_result" | sed -n '1p')
                [ "$login_status" = 200 ] || report_failure "登录页 HTTP $login_status"
                ;;
            *) report_failure '首页未跳转到预期的登录页' ;;
        esac
        ;;
    *) report_failure "HTTP $status" ;;
esac

echo "禅道访问检查通过 ($2，HTTP $status)。"
