# 禅道 Docker 部署

本仓库维护禅道 22.7 的 Docker Compose 配置。从相邻的 [zentaopms](https://github.com/yes8080/zentaopms) 源码检出构建 PHP 8.4 FPM 和 Nginx 1.30，数据库使用 MySQL 8.4。需要 Docker Engine 和支持附加构建上下文的 Docker Compose v2。

## 启动

将两个仓库放在同一父目录，并保留默认目录名。

```sh
git clone https://github.com/yes8080/zentaopms.git
git clone https://github.com/yes8080/zentaopms-docker.git
cd zentaopms-docker
cp .env.example .env
```

在 `.env` 中设置 `MYSQL_PASSWORD` 和 `MYSQL_ROOT_PASSWORD`，然后运行：

```sh
docker compose up -d --build --wait
```

首次启动时，从 8080 端口访问 `/install.php`。数据库主机填 `mysql`，端口填 `3306`，数据库名和应用账号填写 `.env` 中对应的值，表前缀保留 `zt_`。完成安装并创建管理员后，从首页登录。

### 修改监听地址或端口

`ZENTAO_BIND=127.0.0.1` 仅允许从宿主机访问。需要从其他设备访问时，可在 `.env` 中设置 `ZENTAO_BIND=0.0.0.0`，让 Nginx 监听宿主机的所有 IPv4 网卡。宿主机端口由 `ZENTAO_PORT` 指定，默认为 `8080`。

Docker 在创建容器时设置端口绑定。`docker compose restart` 会沿用原绑定。修改 `.env` 后，使用部署时相同的 Compose 文件，只重建 Nginx。默认部署执行：

```sh
docker compose up -d --no-deps --force-recreate nginx
```

同时启用了宿主机目录挂载和 Redis 的部署执行：

```sh
docker compose -f compose.yaml -f compose.persistence.yaml -f compose.redis.yaml \
  up -d --no-deps --force-recreate nginx
```

如果部署时还启用了安全增强配置，也要保留对应的 `-f compose.hardened.yaml`。然后运行 `docker compose ps nginx` 检查 `PORTS` 列。使用上述地址和默认端口时，应显示 `0.0.0.0:8080->80/tcp`。若仍显示旧地址，请检查当前 Shell 是否导出了 `ZENTAO_BIND` 或 `ZENTAO_PORT`。用 `unset ZENTAO_BIND ZENTAO_PORT` 清除覆盖值后，再执行重建命令。

## 可选缓存

默认部署不安装 APCu 或 Redis PHP 扩展，也不启动 Redis 服务。需要缓存时，选择下面一种配置。后续执行同一部署的 Compose 命令时，继续传入对应的配置文件。

**APCu** 适用于单个 PHP 容器，缓存保存在该容器的共享内存中。

```sh
docker compose -f compose.yaml -f compose.apcu.yaml up -d --build --wait
```

**Redis** 启用 PHP Redis 扩展和内部数据网络中的 Redis 服务。Redis 不发布宿主机端口，缓存数据仅保存在内存中。启用前运行 `openssl rand -hex 24`，将结果填入 `.env` 的 `ZT_REDIS_PASSWORD`。密码至少需要 32 个十六进制字符。

```sh
docker compose -f compose.yaml -f compose.redis.yaml up -d --build --wait
```

Redis 镜像同时支持 PHP 内置序列化器和 igbinary。默认使用 `php`，也可以在 `.env` 中设置 `ZT_REDIS_SERIALIZER=igbinary`。升级旧镜像时，先用上面的 `--build` 命令重建。

禅道数据库中已保存的缓存设置优先于容器配置。已有站点在升级到需要 Redis 认证的版本前，先在「后台 → 系统 → 缓存设置」关闭旧 Redis 缓存。升级并启动后，再在后台启用 Redis，主机填 `redis`，端口填 `6379`，数据库选 `0`，用户名留空，密码填 `ZT_REDIS_PASSWORD` 对应的值。切换序列化方式时清理禅道缓存。使用 APCu 时选择共享模式。命名空间可填应用数据库名。


### 排查 Redis 启动失败

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

## 挂载到宿主机目录

默认使用 Docker 命名卷。也可以像官方镜像一样指定一个宿主机目录集中存放持久化数据。这里由独立的 MySQL、PHP 和 Nginx 容器分别挂载所需的子目录。

在 `.env` 中设置 `ZENTAO_DATA_DIR=/data/zentao`，使用 Docker 主机上的绝对路径。先创建三个子目录，再启动服务：

```sh
mkdir -p /data/zentao/mysql /data/zentao/config /data/zentao/data
docker compose -f compose.yaml -f compose.persistence.yaml up -d --build --wait
```

| 子目录 | 内容 |
| --- | --- |
| `mysql/` | MySQL 数据文件 |
| `config/` | 禅道安装生成的 `my.php`，含数据库连接信息 |
| `data/` | 附件和应用数据，Nginx 以只读方式挂载 |

空子目录会在首次启动时初始化。后续更新、重启或停止时继续传入 `-f compose.persistence.yaml`，使用缓存时可以再叠加对应配置文件。例如：

```sh
docker compose -f compose.yaml -f compose.persistence.yaml -f compose.redis.yaml up -d --build --wait
```

已有站点切换前，先备份并把原命名卷中的数据迁入新目录。从官方一体化镜像迁移时，应导出并导入数据库、迁移附件，并核对新容器的数据库连接设置。部署后如需更换宿主机路径，先备份并迁移三个子目录，停止容器并删除对应的三个 `host-*` Docker 卷定义，再用新路径重新启动。[官方 Docker 部署文档](https://www.zentao.net/book/zentaopms/docker-1111.html)

## 部署更新与排查

`zentaopms` 和 `zentaopms-docker` 可以分别更新。构建时会读取同级 `zentaopms` 的当前源码，将应用打包进 PHP 镜像，将 Web 资源打包进 Nginx 镜像。部署新版时，先确认所需改动已拉取到对应的仓库，再在 `zentaopms-docker` 执行 Compose 命令。生产发布时记录两个仓库的提交，便于复现部署。

| 改动 | 应用方式 |
| --- | --- |
| 禅道版本更新，或同时修改 PHP 和 Web 静态资源 | 用 `up -d --build --wait` 更新整套部署。如果版本要求数据库升级，按下文先完成升级再启用安全增强配置。 |
| PHP 业务代码、Redis 驱动、PHP 扩展或镜像内 PHP 配置 | 重建镜像并替换 `php` 容器。 |
| 镜像内的 Nginx 配置 | 重建镜像并替换 `nginx` 容器。 |
| `.env` 中的监听地址或端口 | 只需重建 `nginx` 容器。见[修改监听地址或端口](#修改监听地址或端口)。 |
| Redis 序列化器 | 更新 `.env` 并重建 `php` 容器。已有站点还应核对禅道后台的缓存设置，切换格式时清理旧缓存。 |

以下示例只更新 PHP，适用于已经启动并同时使用宿主机目录挂载和 Redis 的站点：

```sh
docker compose -f compose.yaml -f compose.persistence.yaml -f compose.redis.yaml \
  up -d --build --no-deps php
```

执行时沿用当前部署的项目名和全部 `-f` 配置，包括已启用的安全增强配置。更新后用相同配置执行 `docker compose ps`，再访问受影响的页面验证。排查 Redis 等运行时警告时，在「后台 → 系统 → 错误日志」按时间查看重建后是否还有新增记录，数据库中的历史记录仍会显示。

如果启动时提示 `MYSQL_PASSWORD` 或 `MYSQL_ROOT_PASSWORD` 缺少值，检查 `.env` 中是否填写了这两项，以及 Shell 是否导出了同名变量。用当前部署的 Compose 文件执行 `docker compose config --quiet` 可以验证配置且避免输出密码。MySQL 初始化变量仅在空数据库卷上生效，已有实例更换密码时需修改数据库账号并同步禅道连接配置。

常规镜像更新会复用现有数据卷。迁移已有安装或进行数据库升级前，请备份数据库、应用配置和上传文件。

### 将已有站点升级到 Docker 加固版本

[加固提交 `d54ad08`](https://github.com/yes8080/zentaopms-docker/commit/d54ad080222f142c2d2c1aacfb1df72db5543d73) 同时更新了 PHP、Nginx、Redis 认证和容器网络。需要重新构建 PHP 和 Nginx 镜像，并用完整的 Compose 配置更新整套服务。单独执行 `restart` 或只重建 Nginx 无法应用全部改动。只升级这次 Docker 加固时，可以用同级 `zentaopms` 的当前源码构建。

以下示例适用于已安装禅道、使用 `/data/zentao` 持久化并启用 Redis 的站点。

1. 备份数据库、应用配置和附件。如果旧站使用无密码 Redis，先在「后台 → 系统 → 缓存设置」关闭缓存。
2. 进入 `zentaopms-docker`，执行 `git pull --ff-only origin main`。保留已有 `.env`、数据库凭据、端口和持久化路径。用 `openssl rand -hex 24` 生成 Redis 密码并填入 `ZT_REDIS_PASSWORD`，或者沿用已有的合规密码。
3. 在同一个终端执行以下命令。沿用原部署的 Compose 项目名，如果原来用 `-p` 指定了名称，在函数中加上相同的 `-p`。构建结束后，`up` 替换容器时会有短暂中断，现有数据卷会继续使用。

```sh
dc() {
  docker compose -f compose.yaml -f compose.persistence.yaml \
    -f compose.redis.yaml -f compose.hardened.yaml "$@"
}

dc config --quiet &&
dc pull mysql redis &&
dc build --pull php nginx &&
dc up -d --no-build --wait
```

用 `dc ps` 检查四个服务都已启动，用 `dc exec -T php id -u` 确认 PHP 以 UID `33` 运行。按[排查 Redis 启动失败](#排查-redis-启动失败)一节验证 Redis 返回 `PONG`，然后登录原站、打开历史附件，并用新密码在后台重新启用 Redis。保存后按升级时间检查新增错误日志。

## 安全增强

先按前文完成安装或数据库升级，再启用生产运行配置。首次安装和升级应在仅运维可访问的环境中进行。后续 Compose 命令继续传入安全增强配置文件，使用宿主机挂载或缓存时同时传入相应配置文件。

```sh
docker compose -f compose.yaml -f compose.hardened.yaml build --pull
docker compose -f compose.yaml -f compose.hardened.yaml up -d --no-build --wait
```

- PHP 和 Nginx 以只读根文件系统运行。禅道配置卷只读。PHP 仅为应用临时目录、系统临时目录和应用数据卷保留写权限。缓存、会话和本地日志随 PHP 容器重建清空。禅道内部仍可生成并读取临时 PHP 缓存，临时目录位于 Web 根目录之外。
- Nginx 只将受支持的入口交给 PHP-FPM，拒绝访问 Adminer、隐藏文件、安装与升级入口以及数据目录中的服务端脚本。上传目录设置浏览器沙箱，Nginx 对数据卷保持只读。
- 禅道按解析后的路由阻止在线编辑、二次开发、运行时插件变更和接收客户端 SQL 的图表预览接口。此配置会禁用对应的 BI 预览。需要的扩展由运维审查后随镜像发布。
- 常规登录提交按来源 IP 限流并返回 HTTP 429，所有 PHP 请求另有通用速率限制，禅道保留自身的账号失败次数和锁定机制。对外服务时在可信反向代理终止 HTTPS，限制后端端口只接受该代理访问，并在代理按真实客户端地址统一限流。直接使用 Compose 端口时按连接来源计数。
- MySQL 与 Web 文件隔离。业务账号仅使用应用库的权限，新数据库仅创建本地 root 账号。安全增强模式关闭 general log、服务端文件导入导出和客户端本地文件导入。PHP 镜像构建要求 PHP 8.4.27 或更新版本。
- Nginx 只加入 Web 网络，MySQL 和 Redis 只加入内部数据网络，PHP 加入两者。PHP 以非 root 用户运行，PHP、Nginx 和 Redis 收敛 capabilities，并设置内存和进程数上限。
- 容器标准输出使用轮转日志。需要跨容器重建保留的日志应交给部署环境的集中采集服务。禅道保存在数据库里的错误日志随数据库卷保留。

发布前，从干净的指定源码提交构建，记录两个仓库的提交和镜像摘要。基础镜像固定到摘要，安全更新需更新摘要后重建。发布环境应生成 SBOM，并对最终部署的 PHP、Nginx、MySQL 及可选 Redis 镜像运行漏洞扫描。

使用 Trivy 时，对每个待发布镜像运行以下命令。将 `IMAGE` 设置为实际构建的镜像名称或摘要。

```sh
trivy image --format spdx-json --output image.spdx.json "$IMAGE"
trivy image --scanners vuln --severity HIGH,CRITICAL --exit-code 1 "$IMAGE"
```

历史站点还需在部署时轮换已暴露的密码和密钥、清理测试账号、核查既有 MySQL root 授权及受影响的文件和数据。建议在禅道后台启用登录验证码并提高口令策略。镜像部署不会清理既有数据库里的账号或处理历史入侵。先核查来源目录、扩展及持久化文件，再将历史数据迁入干净实例。
