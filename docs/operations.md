# 更新与恢复

[返回安装指南](../README.md)

## Docker 重启后恢复 Redis 部署

在部署目录的 `.env` 中记录实际使用的完整 Compose 文件集。保留原项目名、数据卷以及覆盖文件的顺序。下面是 Linux 上使用宿主机持久化目录、Redis 和加固配置的示例。使用命名卷时省略 `compose.persistence.yaml`。如果原部署还有其他覆盖文件，将它们按原顺序一并列出。

```dotenv
COMPOSE_FILE=compose.yaml:compose.persistence.yaml:compose.redis.yaml:compose.hardened.yaml
```

将配置写入现有 `.env`，替换同名行。在终端单独输入 `COMPOSE_FILE=...` 只设置 Shell 变量。临时设置可用 `export COMPOSE_FILE=...`。保存 `.env` 后，清除终端中的变量并验证持久配置：

```sh
unset COMPOSE_FILE
docker compose config --services
grep -E '^(ZENTAO_DATA_DIR|COMPOSE_FILE)=' .env
```

确认列出 `mysql`、`redis`、`php`、`nginx`。使用宿主机持久化时，同时核对 `ZENTAO_DATA_DIR` 仍指向原数据目录。若文件清单有疑问，可对原 Redis 容器执行 `docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' <容器名>`，核对文件和顺序。

如果普通 `docker compose up` 创建了新的命名卷、把原 Redis 标为孤立容器，或原站点出现安装向导，先核对原文件集、项目名和数据目录。核对前保留现有卷和容器，避免初始化新库或执行 `--remove-orphans`、`down -v`。

在部署目录执行以下命令。脚本先确认四个服务都在生效的 Compose 配置中，然后等待服务就绪并检查站点入口。使用了自定义端口时，请传入实际地址。

```sh
./scripts/recover-redis-site.sh http://127.0.0.1:8080
```

恢复脚本适用于配置和镜像已经匹配的站点。从普通配置切回加固配置、变更构建参数或更新应用镜像时，先按上面的加固切换步骤重新构建 PHP 和 Nginx。首页 HTTP 302 只能验证站点入口；如果 PHP 日志仍新增删除 `install.php`、`upgrade.php` 的只读警告，请先核对运行镜像，再用完整文件集重建。

Redis 配置为四个服务启用了 `restart: unless-stopped`。应用这次配置时使用完整文件集执行恢复脚本，Compose 会更新已有容器的配置。Docker 恢复后可自动启动此前运行的容器，手动停止的容器仍需显式启动。自动启动没有 Compose 的 Redis 健康等待顺序，短暂不可用时应运行上述脚本检查。运行中 Redis 断连仍可能使 PHP 请求失败。首次应用重启策略请使用恢复脚本，`docker compose restart` 只重启现有容器。恢复后核对四个容器的 `RestartPolicy.Name` 均为 `unless-stopped`，并登录确认原有项目和附件可读。

## 部署更新

`zentaopms` 和 `zentaopms-docker` 可以分别更新。构建时会读取同级 `zentaopms` 的当前源码，将应用打包进 PHP 镜像，将 Web 资源打包进 Nginx 镜像。部署新版时，先确认所需改动已拉取到对应的仓库，再在 `zentaopms-docker` 执行 Compose 命令。生产发布时记录两个仓库的提交，便于复现部署。

| 改动 | 应用方式 |
| --- | --- |
| 禅道版本更新，或同时修改 PHP 和 Web 静态资源 | 用 `up -d --build --wait` 更新整套部署。如果版本要求数据库升级，按下文先完成升级再启用安全增强配置。 |
| PHP 业务代码、Redis 驱动、PHP 扩展或镜像内 PHP 配置 | 重建镜像并替换 `php` 容器。 |
| 镜像内的 Nginx 配置 | 重建镜像并替换 `nginx` 容器。 |
| `.env` 中的监听地址或端口 | 只需重建 `nginx` 容器。见[修改监听地址或端口](configuration.md#修改监听地址或端口)。 |
| Redis 序列化器 | 更新 `.env` 并重建 `php` 容器。已有站点还应核对禅道后台的缓存设置，切换格式时清理旧缓存。 |

如果已按安装或恢复指南将完整文件集写入 `.env`，只更新 PHP 时执行：

```sh
docker compose config --quiet &&
docker compose up -d --build --no-deps php &&
docker compose ps
```

执行前核对当前项目和文件集，包括已启用的安全增强配置。更新后访问受影响的页面，在「后台 → 系统 → 错误日志」按重建时间查看新增记录。

如果启动时提示 `MYSQL_PASSWORD` 或 `MYSQL_ROOT_PASSWORD` 缺少值，检查 `.env` 中是否填写了这两项，以及 Shell 是否导出了同名变量。用当前部署的 Compose 文件执行 `docker compose config --quiet` 可以验证配置且避免输出密码。MySQL 初始化变量仅在空数据库卷上生效，已有实例更换密码时需修改数据库账号并同步禅道连接配置。

常规镜像更新会复用现有数据卷。迁移已有安装或进行数据库升级前，请备份数据库、应用配置和上传文件。

### 将已有站点升级到 Docker 加固版本

[加固提交 `d54ad08`](https://github.com/yes8080/zentaopms-docker/commit/d54ad080222f142c2d2c1aacfb1df72db5543d73) 同时更新了 PHP、Nginx、Redis 认证和容器网络。需要重新构建 PHP 和 Nginx 镜像，并用完整的 Compose 配置更新整套服务。单独执行 `restart` 或只重建 Nginx 无法应用全部改动。只升级这次 Docker 加固时，可以用同级 `zentaopms` 的当前源码构建。

以下示例适用于已安装禅道、使用 `/data/zentao` 持久化并启用 Redis 的站点。

1. 备份数据库、应用配置和附件。如果旧站使用无密码 Redis，先在「后台 → 系统 → 缓存设置」关闭缓存。
2. 进入 `zentaopms-docker`，执行 `git pull --ff-only origin main`。保留已有 `.env`、数据库凭据、端口和持久化路径。用 `openssl rand -hex 24` 生成 Redis 密码并填入 `ZT_REDIS_PASSWORD`，或者沿用已有的合规密码。
3. 在同一个终端执行以下命令。沿用原部署的 Compose 项目名，如果原来用 `-p` 指定了名称，在函数中加上相同的 `-p`。构建完成后，在维护窗口停止旧容器并重建项目网络，现有数据卷会继续使用。此停机步骤用于迁移网络拓扑的升级，常规镜像升级仍使用 `up`。

```sh
dc() {
  docker compose -f compose.yaml -f compose.persistence.yaml \
    -f compose.redis.yaml -f compose.hardened.yaml "$@"
}

dc config --quiet &&
dc pull mysql redis &&
dc build --pull php nginx &&
dc down &&
dc up -d --no-build --wait &&
./scripts/check-deployment.sh http://127.0.0.1:8080 site
```

保留原来的 Compose 项目名和持久化路径。`down` 不加 `-v`。检查地址应使用宿主机实际发布的地址和端口。

用 `dc ps` 检查四个服务都已启动，用 `dc exec -T php id -u` 确认 PHP 以 UID `33` 运行。按[排查 Redis 启动失败](troubleshooting.md#排查-redis-启动失败)一节验证 Redis 返回 `PONG`，再从 PHP 容器确认能用同一密码连接 Redis。然后登录原站、打开历史附件，并用新密码在后台重新启用 Redis。

```sh
dc exec -T php php -r '
$r = new Redis();
if (!$r->connect("redis", 6379, 3) || !$r->auth(getenv("ZT_REDIS_PASSWORD"))) exit(1);
$key = "deployment-check:" . bin2hex(random_bytes(8));
if (!$r->setex($key, 30, "ok") || $r->get($key) !== "ok") exit(1);
echo "PHP Redis read/write OK", PHP_EOL;
'
```

保存后按升级时间检查新增错误日志。

## 发布与运维检查

发布前，从干净的指定源码提交构建，记录两个仓库的提交和镜像摘要。基础镜像固定到摘要，安全更新需更新摘要后重建。发布环境应生成 SBOM，并对最终部署的 PHP、Nginx、MySQL 及可选 Redis 镜像运行漏洞扫描。

使用 Trivy 时，对每个待发布镜像运行以下命令。将 `IMAGE` 设置为实际构建的镜像名称或摘要。

```sh
trivy image --format spdx-json --output image.spdx.json "$IMAGE"
trivy image --scanners vuln --severity HIGH,CRITICAL --exit-code 1 "$IMAGE"
```

如果默认漏洞数据库镜像站不可达，且构建机可以访问官方 GHCR 仓库，先下载数据库，再使用同一缓存扫描。代理使用内部 CA 时，应让运行 Trivy 的进程信任该 CA。

```sh
trivy image --download-db-only --db-repository ghcr.io/aquasecurity/trivy-db:2
trivy image --skip-db-update --scanners vuln --severity HIGH,CRITICAL --exit-code 1 "$IMAGE"
```

使用离线缓存前先检查数据库的更新时间。下载失败时，发布验收仍需完成漏洞扫描。


历史站点还需在部署时轮换已暴露的密码和密钥、清理测试账号、核查既有 MySQL root 授权及受影响的文件和数据。建议在禅道后台启用登录验证码并提高口令策略。镜像部署不会清理既有数据库里的账号或处理历史入侵。先核查来源目录、扩展及持久化文件，再将历史数据迁入干净实例。
