# 故障排查

[返回安装指南](../README.md)

## 排查 Redis 启动失败

如果启动时出现 `dependency failed to start`，且 Redis 以退出码 `1` 停止，先查看 Redis 日志。密码含非十六进制字符或少于 32 个字符时，Redis 会退出，依赖它的 PHP 和 Nginx 也无法启动。用 `openssl rand -hex 24` 生成 48 个十六进制字符，将结果填入现有 `.env` 的 `ZT_REDIS_PASSWORD`。

以下命令适用于同时启用宿主机持久化、Redis 和安全增强的站点。沿用部署时的项目名和 Compose 文件。首次切换到安全增强配置时，先确认站点已完成安装或数据库升级。

```sh
dc() {
  docker compose -f compose.yaml -f compose.persistence.yaml \
    -f compose.redis.yaml -f compose.hardened.yaml "$@"
}
dc logs --tail=30 redis
dc up -d --build --wait
dc ps
dc exec -T redis sh -ec 'REDISCLI_AUTH="$ZT_REDIS_PASSWORD" redis-cli ping'
```

最后一条命令应返回 `PONG`。随后按上文在禅道后台保存 Redis 设置，使用与 `.env` 相同的密码，再刷新页面并检查新增错误日志。

恢复默认缓存配置前，先在禅道后台关闭缓存，再省略缓存配置文件并添加 `--remove-orphans`。若正在使用宿主机目录挂载，继续传入 `-f compose.persistence.yaml`。下面的命令适用于默认命名卷部署，也会清理先前启用的 Redis 容器。

```sh
docker compose up -d --build --wait --remove-orphans
```

## 排查容器已启动但首页返回 504

`up --wait` 检查容器状态。从宿主机运行 `./scripts/check-deployment.sh http://127.0.0.1:8080 site` 验证 HTTP 链路。检查通过后，再用已有账号登录、打开历史附件并核对缓存设置。

加固模式下，如果 `/install.php` 立即返回 `404`，而首页超时或返回 `504`，用部署时相同的 Compose 文件查看 `logs --tail=80 nginx php mysql redis`。未启用 Redis 时省略 `redis`。如果 Nginx 日志显示 `while connecting to upstream`，先用 `docker network inspect` 核对网络成员。正常情况下 Nginx 和 PHP 在 `web` 网络，PHP、MySQL 和可选 Redis 在 `data` 网络。用以下命令比较重新访问首页前后的转发计数。即使请求超时，也继续执行下一条命令。

```sh
diag_dir=$(mktemp -d)
sudo iptables-save -c -t filter > "$diag_dir/before"
curl --noproxy '*' -sS --max-time 5 -o /dev/null http://127.0.0.1:8080/
sudo iptables-save -c -t filter > "$diag_dir/after"
diff -u "$diag_dir/before" "$diag_dir/after"
```

重点查看 `FORWARD` 和 `DOCKER-FORWARD` 的计数。使用原生 nftables 时检查相应的 forward 链。

如果已确认请求在宿主机转发链被丢弃，且旧网桥缺少 Docker 生成的规则，可在维护窗口按上文重建当前项目的容器和网络。使用已有镜像时只需 `dc down` 和 `dc up -d --no-build --wait`，随后重新运行宿主机验收。保留持久卷及原有防火墙策略。

## 安全增强的功能限制与排查

启用前请核对站点依赖的插件、接口和上传方式。下表适用于叠加 `compose.hardened.yaml` 的部署。

| 现象或受影响的功能 | 原因与排查方法 |
| --- | --- |
| 安装页或升级页返回 `404` | 加固镜像移除了 `/install.php` 和 `/upgrade.php`。全新部署先按[首次安装流程](../README.md#全新部署持久化--redis--安全增强)完成安装。需要执行数据库升级时，在仅运维可访问的环境用普通配置完成升级，再恢复加固配置。已安装站点用验收脚本的 `site` 模式。 |
| Adminer、自定义 PHP 入口、隐藏文件或上传的脚本返回 `404` | 公开 PHP 入口为 `index.php`、`api.php`、`x.php` 和 `imgproxy.php`。附件直链由网关指定的内部 PHP 脚本校验，数据目录中的脚本禁止执行。核对访问路径和 Nginx 日志。通过已支持的入口集成，需要新增入口时在镜像发布前审查。上传目录还会发送浏览器沙箱策略，依赖脚本的 HTML 预览可能受限。 |
| 在线编辑、二次开发、安装或卸载插件、部分 BI 图表预览返回 `403` | 应用路由限制了 `editor`、`dev`、除浏览和结构查看外的 `extension` 操作，以及 `chart.ajaxGetChart`。旧客户端通过 URL 查询参数提交账号或密码的登录请求也会返回 `403`。根据请求时间、路由类别和状态码对照访问日志。插件经运维审查后随镜像发布，客户端改用受支持的登录方式。 |
| 登录频繁时或批量调用接口时返回 `429` | 网页登录和 API v1 令牌签发按来源 IP 限速，每分钟 10 次并允许 5 次突发。常规动态请求受每秒 10 次、突发 30 次的通用限制，附件读取使用独立配额。查看访问日志中的 `limit=REJECTED`，并确认经反向代理的多名用户是否共用同一个来源 IP。禅道自己的账号锁定仍要在后台单独检查。 |
| 上传大文件失败或返回 `413` | 加固 Nginx 的请求体上限为 16 MiB。仓库共用的 PHP 配置还限制单个上传文件为 10 MiB、整个 POST 为 16 MiB。先看哪一层记录了拒绝，再核对反向代理、Nginx 和 PHP 的限制。修改镜像内配置后需重建对应镜像。 |
| 保存配置、写入自定义目录或运行时安装扩展报权限错误 | PHP 以 UID/GID `33:33` 运行，根文件系统和配置卷只读。核对目标路径、挂载和宿主机文件权限，确认应用数据目录对 PHP 可写、配置文件可读。代码、配置文件和扩展通过部署更新。 |
| 重建容器后会话、临时缓存或本地日志消失 | PHP 临时目录使用 `tmpfs`。用户可能需要重新登录，应用会重建缓存。部署前如需保留日志，应通过容器日志或集中采集系统留存。数据库内的错误日志随数据库卷保留。 |
| SQL 文件导入导出失败，或无法从宿主机直连数据库和 Redis | 加固 MySQL 禁用 `LOCAL INFILE` 及服务端文件导入导出。需要运维导出时，在 MySQL 容器内使用 `mysqldump` 等客户端。当前部署的 MySQL 和 Redis 只在内部数据网络提供服务。若首页 `502` 或 `504`，按[容器网络排查](#排查容器已启动但首页返回-504)检查。 |
| 并发任务重、容器退出或进程创建失败 | 加固为 MySQL、PHP 和 Nginx 设置了内存及进程数上限，启用 Redis 时它也有自己的上限。用 `scripts/check-runtime-limits.sh` 核实内核执行的限额，再用 `docker stats` 和 `docker inspect` 检查资源使用、退出状态及 `OOMKilled`。 |

排查时沿用部署时的项目名和全部 Compose 文件。先记录失败的时间、路径和 HTTP 状态，再查看 Nginx 和 PHP 日志。加固站点可先从宿主机运行 `./scripts/check-deployment.sh http://127.0.0.1:8080 site`，把入口连通性和具体业务功能分开检查。

## 持久化配置卷名称不匹配

如果 Compose 提示旧 `app-config` 卷标签匹配但名称不同，先查看 PHP 当前挂载和新卷的宿主机路径：

```sh
docker inspect "$(docker compose ps -q php)" \
  --format '{{range .Mounts}}{{println .Name "->" .Destination}}{{end}}'
```

宿主机持久化部署应将 `<项目名>_host-app-config` 挂到 `/var/lib/zentao/config`。用 `docker volume inspect <卷名> --format '{{index .Options "device"}}'` 核对它指向原来的配置目录，例如 `/data/zentao/config`。登录确认原项目和附件可读后，可以保留旧卷。需要清理时，先确认旧卷未被任何容器引用且其中没有需要保留的配置。

### PHP 缓存扩展下载失败

PHP 镜像从各扩展的 GitHub 上游仓库下载锁定版本的源码归档，校验 SHA-256 后编译。构建主机需要允许访问 `codeload.github.com`。如果代理使用内部 CA，并且构建报证书链错误，可以创建以下本地 Compose 覆盖文件。将 `file` 指向构建机上包含代理 CA 的受信任证书包。

```yaml
services:
  php:
    build:
      secrets:
        - apt_ca_bundle
secrets:
  apt_ca_bundle:
    file: /etc/ssl/certs/ca-certificates.crt
```

手工构建时，将此文件追加到完整的 Compose 文件列表。使用 `zentaoctl install` 时，在实例环境文件的 `COMPOSE_FILE` 中先列出与所选持久化和缓存模式匹配的配置，再追加此覆盖文件。例如 Redis 加宿主机持久化的新装可用 `COMPOSE_FILE=compose.yaml:compose.persistence.yaml:compose.redis.yaml:/绝对路径/compose.build-ca.yaml`。
