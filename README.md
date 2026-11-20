# 禅道 Docker 部署

本仓库维护禅道 22.7 的 Docker Compose 配置。部署镜像运行 PHP-FPM，RoadRunner worker、其专用 cron 入口、商业扩展专用的 SheetJS 和旧 Vue 2 资源已从构建上下文排除。大屏模块继续使用自带的 Vue 3。从相邻的 [zentaopms](https://github.com/yes8080/zentaopms) 源码检出构建 PHP 8.4 FPM 和 Nginx 1.30，数据库使用 MySQL 8.4。需要 Docker Engine 和支持附加构建上下文、环境变量来源 secrets 的 Docker Compose v2。本项目在 Compose v2.40.3 上验证。

## 启动

> [!IMPORTANT]
> **全新安装必须先完成安装向导，再启用安全增强。** 首次启动时省略 `-f compose.hardened.yaml`，访问 `/install.php` 完成安装并确认管理员可以登录。加固版 PHP 镜像移除了安装入口，启用后访问 `/install.php` 会返回 `404`，无法执行全新安装。已安装的加固站点请用验收脚本的 `site` 模式检查。

需要宿主机持久化、Redis 和安全增强时，完成下方仓库准备后，直接进入[全新部署流程](#全新部署持久化--redis--安全增强)。

将两个仓库放在同一父目录，并保留默认目录名。

```sh
git clone https://github.com/yes8080/zentaopms.git
git clone https://github.com/yes8080/zentaopms-docker.git
cd zentaopms-docker
cp .env.example .env
```

### 默认快速启动

在 `.env` 中设置 `MYSQL_PASSWORD` 和 `MYSQL_ROOT_PASSWORD`，然后运行：

```sh
docker compose up -d --build --wait
```

启动后，在宿主机运行 `./scripts/check-deployment.sh http://127.0.0.1:8080 install` 检查安装入口。如果修改了监听地址或端口，请替换为实际地址。

首次启动时，从 8080 端口访问 `/install.php`。数据库主机填 `mysql`，端口填 `3306`，数据库名和应用账号填写 `.env` 中对应的值，表前缀保留 `zt_`。完成安装并创建管理员后，用同一脚本的 `site` 模式检查首页，再登录。

### 全新部署：持久化 + Redis + 安全增强

按上面的步骤克隆两个仓库并创建 `.env`，然后选择本节流程。示例使用宿主机目录 `/data/zentao` 和本机端口 `8080`。

#### 1. 准备持久化目录和密码

```sh
mkdir -p /data/zentao/mysql /data/zentao/config /data/zentao/data
```

编辑 `.env`，设置 `ZENTAO_DATA_DIR=/data/zentao`、`MYSQL_PASSWORD` 和 `MYSQL_ROOT_PASSWORD`。运行 `openssl rand -hex 24`，将生成的 48 个十六进制字符填入 `ZT_REDIS_PASSWORD`。安装期间保持默认监听地址 `127.0.0.1`。如果修改了端口，也要替换下面验收命令里的端口。

#### 2. 启动服务并完成安装

在 `zentaopms-docker` 目录运行：

```sh
docker compose -f compose.yaml -f compose.persistence.yaml -f compose.redis.yaml \
  up -d --build --wait &&
./scripts/check-deployment.sh http://127.0.0.1:8080 install
```

> [!IMPORTANT]
> 如果是未完成安装的新站点，且误先启用了安全增强，请沿用原项目名和持久化目录，执行上面的三文件命令（含 `--build`）切回安装阶段。安装完成后再执行第 3 步。

在宿主机打开 `http://127.0.0.1:8080/install.php`。数据库主机填 `mysql`，端口填 `3306`，数据库名、业务账号和密码使用 `.env` 中的 `MYSQL_DATABASE`、`MYSQL_USER` 和 `MYSQL_PASSWORD`，表前缀保留 `zt_`。完成安装、创建管理员并确认能登录后，再切换到加固配置。

#### 3. 启用安全增强并验收

启用前请查看[安全增强的功能限制与排查](#安全增强的功能限制与排查)。

沿用同一目录、项目名和 `.env`，在原有三个文件后叠加 `compose.hardened.yaml`：

```sh
dc() {
  docker compose -f compose.yaml -f compose.persistence.yaml \
    -f compose.redis.yaml -f compose.hardened.yaml "$@"
}
dc config --quiet &&
dc build --pull mysql php nginx &&
dc up -d --no-build --wait &&
./scripts/check-deployment.sh http://127.0.0.1:8080 site
```

切换后会继续使用原数据库、配置和附件目录。加固配置关闭安装入口，访问 `/install.php` 应返回 `404`。用刚创建的管理员登录，按[可选缓存](#可选缓存)一节核对后台 Redis 设置，并按[加固升级验收](#将已有站点升级到-docker-加固版本)一节验证 PHP 到 Redis 的认证和读写。

如果 Redis 无法启动，参见[排查 Redis 启动失败](#排查-redis-启动失败)。如果容器已启动，但宿主机验收超时或返回 `504`，参见[排查容器已启动但首页返回 504](#排查容器已启动但首页返回-504)。

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

### 已安装 22.7 站点的认证与集成迁移

先记录当前镜像 ID。维护窗口内停止 Web 写入，备份数据库、配置卷和附件卷；旧容器中的内置备份如尚未导出，在替换容器前复制 `/var/www/zentao/tmp/backup`。新部署将该路径挂到项目的 `app-backup` 卷。即使应用临时目录使用 tmpfs，也可跨容器重建保留备份。自定义过备份目录的站点应单独持久化该目录。异机恢复仍需导出备份并妥善保管解密所需凭据。

使用实际项目名、原有数据卷及全部部署文件启动 MySQL，保持 PHP 和 Nginx 停止。下面的命令适用于默认 MySQL 表名与新版 Compose；如现有业务表前缀不同，先核对 SQL 里的表名。新装 22.7 已包含这两份结构变更。

```sh
docker compose build
docker compose stop nginx php
docker compose up -d --no-build --wait mysql
( set -e
for migration in ../zentaopms/db/secure-user-passwords.sql ../zentaopms/db/secure-provider-credentials.sql; do
  docker compose exec -T mysql sh -ec 'MYSQL_PWD="$(cat "$MYSQL_ROOT_PASSWORD_FILE")" exec mysql -u root "$MYSQL_DATABASE"' < "$migration"
done
)
```

确认两份结构迁移成功后，按下文迁移供应方令牌，再启动 Web 服务。旧账号使用原密码登录一次，服务端会把旧摘要迁移为 Argon2id。旧的“保持登录”凭据失效。登录客户端使用 HTTPS 请求正文传递密码；本地开发可通过回环访问。回滚时配套恢复旧镜像及切换前的数据备份，已迁移的密码无法转换回旧摘要。

MySQL 从 Compose secret 文件读取两个数据库口令。原有 `.env` 变量名继续使用，请以仅部署账号可读的权限保存此文件。若构建环境需信任企业代理 CA，在构建命令末个 `-f` 位置加上 `-f compose.build-ca.yaml`；可通过 `ZENTAO_BUILD_CA_FILE` 选择宿主机 CA bundle。

存储带令牌的供应方或使用 GitLab 回调时，叠加 `-f compose.integrations.yaml`。已启用集成加密的站点沿用原主密钥。首次启用时，建立仅部署账号可进入的目录，生成一次独立主密钥，将文件绝对路径配置到 `.env` 的 `ZENTAO_INTEGRATION_KEY_PATH`。以下命令创建新目录并防止覆盖已有密钥。在受限目录内给密钥文件读权限，以便以 UID 33 运行的 PHP 读取容器内的单个只读挂载。

```sh
( set -euC; umask 077; mkdir -m 700 ./integration-secrets; openssl rand -hex 32 > ./integration-secrets/integration.key; chmod 0444 ./integration-secrets/integration.key )
```

使用部署时相同的 Compose 文件、数据库和主密钥执行以下命令。保持 Nginx、其他应用副本和会写这个数据库的后台作业停止，直至第二次检查显示待迁移数为零。检查或迁移失败时保留维护窗口，核对表结构与原密钥后重试。没有已存凭据的实例也可以执行检查。

```sh
docker compose up -d --no-build --no-deps --wait php
docker compose exec -T php php bin/php/migrate-provider-tokens.php --check &&
docker compose exec -T php php bin/php/migrate-provider-tokens.php --apply &&
docker compose exec -T php php bin/php/migrate-provider-tokens.php --check &&
docker compose up -d --no-build --wait
```

迁移会在单个事务中加密历史明文令牌及 Jenkins 旧编码凭据，并校验已加密的记录。将主密钥与业务数据库分开保管及备份；回滚时恢复切换前的数据库、配置、对应的镜像和密钥。历史数据库备份仍可能包含可复用的令牌。对接服务若需要轮换凭据，先签发新令牌，在禅道保存并验证，再撤销旧令牌。

GitLab 回调另需设置 `ZT_GITLAB_CALLBACK_BASE_URL` 为运维管理的 HTTPS 站点地址（含实际子路径），再编辑保存相应流水线以更新远端回调。手工配置的仓库回调可在受控终端执行 `docker compose exec -T php php bin/php/gitlab-repo-hook-token.php <禅道仓库ID>` 获取该仓库专用值，录入对应 GitLab 回调的 Secret Token。将输出作为密钥保管。更换主密钥前安排重新登记回调与服务凭据。企业内部 TLS 服务需给 PHP 容器配置受信任 CA；构建阶段使用的 apt CA 不会自动成为应用运行时的信任库。

### 将已有站点升级到 Docker 加固版本

[加固提交 `d54ad08`](https://github.com/yes8080/zentaopms-docker/commit/d54ad080222f142c2d2c1aacfb1df72db5543d73) 同时更新了 PHP、Nginx、Redis 认证和容器网络。需要重新构建 PHP 和 Nginx 镜像，并用完整的 Compose 配置更新整套服务。单独执行 `restart` 或只重建 Nginx 无法应用全部改动。只升级这次 Docker 加固时，可以用同级 `zentaopms` 的当前源码构建。

以下示例适用于已安装禅道、使用 `/data/zentao` 持久化并启用 Redis 的站点。

1. 备份数据库、应用配置和附件。如果旧站使用无密码 Redis，先在「后台 → 系统 → 缓存设置」关闭缓存。
2. 进入 `zentaopms-docker`，执行 `git pull --ff-only origin main`。保留已有 `.env`、数据库凭据、端口和持久化路径。已有 22.7 数据库还须先执行上文的认证与集成迁移。用 `openssl rand -hex 24` 生成 Redis 密码并填入 `ZT_REDIS_PASSWORD`，或者沿用已有的合规密码。
3. 在同一个终端执行以下命令。沿用原部署的 Compose 项目名，如果原来用 `-p` 指定了名称，在函数中加上相同的 `-p`。构建完成后，在维护窗口停止旧容器并重建项目网络，现有数据卷会继续使用。此停机步骤用于迁移网络拓扑的升级，常规镜像升级仍使用 `up`。

```sh
dc() {
  docker compose -f compose.yaml -f compose.persistence.yaml \
    -f compose.redis.yaml -f compose.hardened.yaml "$@"
}

dc config --quiet &&
dc pull redis &&
dc build --pull mysql php nginx &&
dc down &&
dc up -d --no-build --wait &&
./scripts/check-deployment.sh http://127.0.0.1:8080 site
```

保留原来的 Compose 项目名和持久化路径。`down` 不加 `-v`。检查地址应使用宿主机实际发布的地址和端口。

用 `dc ps` 检查四个服务都已启动，用 `dc exec -T php id -u` 确认 PHP 以 UID `33` 运行。按[排查 Redis 启动失败](#排查-redis-启动失败)一节验证 Redis 返回 `PONG`，再从 PHP 容器确认能用同一密码连接 Redis。然后登录原站、打开历史附件，并用新密码在后台重新启用 Redis。

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

### 排查容器已启动但首页返回 504

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

## 安全增强

先按前文完成安装或数据库升级，再启用生产运行配置。首次安装和升级应在仅运维可访问的环境中进行。后续 Compose 命令继续传入安全增强配置文件，使用宿主机挂载或缓存时同时传入相应配置文件。

```sh
docker compose -f compose.yaml -f compose.hardened.yaml build --pull
docker compose -f compose.yaml -f compose.hardened.yaml up -d --no-build --wait
```

PHP 基于固定摘要的 Debian 13 官方 FPM 镜像，构建时从签名仓库安装可用更新。可访问 Alpine 和 Oracle Linux 官方软件源的构建机，还可以叠加系统更新配置，对加固版 Nginx 和 MySQL 刷新系统包。以下命令沿用同一个 Compose 项目，若使用自定义 CA，将 `-f compose.build-ca.yaml` 加在其他文件之后。构建成功后先复扫最终镜像，再用同一组文件在备份副本上验收。

```sh
docker compose -f compose.yaml -f compose.hardened.yaml -f compose.os-updates.yaml \
  build --pull --no-cache mysql nginx
```

该配置保留包签名与 TLS 校验。软件源不可访问会中止构建。MySQL 检查已公布的 Oracle 安全公告所要求的最低包版本。未叠加时两者使用固定基础镜像自带的系统包。

- PHP 和 Nginx 以只读根文件系统运行。禅道配置卷只读。PHP 仅为应用临时目录、系统临时目录和应用数据卷保留写权限。缓存、会话和本地日志随 PHP 容器重建清空。禅道内部仍可生成并读取临时 PHP 缓存，临时目录位于 Web 根目录之外。
- Nginx 只将受支持的入口交给 PHP-FPM，拒绝访问 Adminer、隐藏文件、安装与升级入口以及数据目录中的服务端脚本。上传目录设置浏览器沙箱，Nginx 对数据卷保持只读。
- 禅道按解析后的路由阻止在线编辑、二次开发、运行时插件变更和接收客户端 SQL 的图表预览接口。此配置会禁用对应的 BI 预览。需要的扩展由运维审查后随镜像发布。
- 常规登录提交按来源 IP 限流并返回 HTTP 429，所有 PHP 请求另有通用速率限制，禅道保留自身的账号失败次数和锁定机制。对外服务时在可信反向代理终止 HTTPS，限制后端端口只接受该代理访问，并在代理按真实客户端地址统一限流。直接使用 Compose 端口时按连接来源计数。
- 加固数据库镜像使用固定摘要的 Go 工具链从校验过的上游源码重建 gosu 1.20，保留官方 MySQL 入口、mysql、mysqldump 和 mysqladmin，移除服务运行不需要的 mysqlsh。基础镜像和加固派生镜像应同时复扫。MySQL 与 Web 文件隔离。业务账号仅使用应用库的权限，新数据库仅创建本地 root 账号。安全增强模式关闭 general log、服务端文件导入导出和客户端本地文件导入。PHP 镜像构建要求 PHP 8.4.27 或更新版本。
- PHP 健康检查验证 FPM 监听端口，Nginx 健康检查读取本机静态资源。应用访问通过宿主机验收脚本及真实登录单独验证。Nginx 只加入 Web 网络，MySQL 和 Redis 只加入内部数据网络，PHP 加入两者。PHP 和 Nginx 分别以 UID 33 和 101 运行，两个容器均移除全部 Linux capabilities；Redis 收敛 capabilities，并设置内存和进程数上限。
- 容器标准输出使用轮转日志。需要跨容器重建保留的日志应交给部署环境的集中采集服务。禅道保存在数据库里的错误日志随数据库卷保留。

### 安全增强的功能限制与排查

启用前请核对站点依赖的插件、接口和上传方式。下表适用于叠加 `compose.hardened.yaml` 的部署。

| 现象或受影响的功能 | 原因与排查方法 |
| --- | --- |
| 安装页或升级页返回 `404` | 加固镜像移除了 `/install.php` 和 `/upgrade.php`。全新部署先按[首次安装流程](#全新部署持久化--redis--安全增强)完成安装。需要执行数据库升级时，在仅运维可访问的环境用普通配置完成升级，再恢复加固配置。已安装站点用验收脚本的 `site` 模式。 |
| Adminer、自定义 PHP 入口、隐藏文件或上传的脚本返回 `404` | Nginx 只把 `index.php`、`api.php`、`x.php` 和 `imgproxy.php` 交给 PHP-FPM，并拒绝从数据目录执行脚本。核对访问路径和 Nginx 日志。通过已支持的入口集成，需要新增入口时在镜像发布前审查。上传目录还会发送浏览器沙箱策略，依赖脚本的 HTML 预览可能受限。 |
| 在线编辑、二次开发、安装或卸载插件、部分 BI 图表预览返回 `403` | 应用路由限制了 `editor`、`dev`、除浏览和结构查看外的 `extension` 操作，以及 `chart.ajaxGetChart`。旧客户端通过 URL 查询参数提交账号或密码的登录请求也会返回 `403`。对照访问日志中的路由，插件经运维审查后随镜像发布，客户端改用受支持的登录方式。 |
| 登录频繁时或批量调用接口时返回 `429` | 登录提交按来源 IP 限速，每分钟 10 次并允许 5 次突发。PHP 请求另有每秒 10 次和 30 次突发的限制。查看 Nginx 日志中的 `limiting requests`，并确认经反向代理的多名用户是否共用同一个来源 IP。禅道自己的账号锁定仍要在后台单独检查。 |
| 上传大文件失败或返回 `413` | 加固 Nginx 的请求体上限为 16 MiB。仓库共用的 PHP 配置还限制单个上传文件为 10 MiB、整个 POST 为 16 MiB。先看哪一层记录了拒绝，再核对反向代理、Nginx 和 PHP 的限制。修改镜像内配置后需重建对应镜像。 |
| 保存配置、写入自定义目录或运行时安装扩展报权限错误 | PHP 以 UID/GID `33:33` 运行，根文件系统和配置卷只读。核对目标路径、挂载和宿主机文件权限，确认应用数据目录对 PHP 可写、配置文件可读。代码、配置文件和扩展通过部署更新。 |
| 重建容器后会话、临时缓存或本地日志消失 | PHP 临时目录使用 `tmpfs`。用户可能需要重新登录，应用会重建缓存。部署前如需保留日志，应通过容器日志或集中采集系统留存。数据库内的错误日志随数据库卷保留。 |
| SQL 文件导入导出失败，或无法从宿主机直连数据库和 Redis | 加固 MySQL 禁用 `LOCAL INFILE` 及服务端文件导入导出。需要运维导出时，在 MySQL 容器内使用 `mysqldump` 等客户端。当前部署的 MySQL 和 Redis 只在内部数据网络提供服务。若首页 `502` 或 `504`，按[容器网络排查](#排查容器已启动但首页返回-504)检查。 |
| 并发任务重、容器退出或进程创建失败 | 加固为 MySQL、PHP 和 Nginx 设置了内存及进程数上限，启用 Redis 时它也有自己的上限。使用 `docker stats` 和 `docker inspect` 检查资源使用、退出状态及 `OOMKilled`，结合容器日志判断。 |

排查时沿用部署时的项目名和全部 Compose 文件。先记录失败的时间、路径和 HTTP 状态，再查看 Nginx 和 PHP 日志。加固站点可先从宿主机运行 `./scripts/check-deployment.sh http://127.0.0.1:8080 site`，把入口连通性和具体业务功能分开检查。

### 发布与运维检查

发布前，从干净的指定源码提交构建，记录两个仓库的提交和镜像摘要。基础镜像固定到摘要，安全更新需更新摘要后重建。发布环境应生成 SBOM，并对最终部署的 PHP、Nginx、MySQL 及可选 Redis 镜像运行漏洞扫描。

在构建好的 PHP 容器中可复跑写请求校验和备份文件边界测试。测试使用独立的临时数据。

```sh
docker compose exec -T php php /dev/stdin /var/www/zentao < scripts/check-write-guards.php
docker compose exec -T php php /dev/stdin /var/www/zentao < scripts/check-backup-paths.php
```

使用 Trivy 时，对每个待发布镜像运行以下命令。将 `IMAGE` 设置为实际构建的镜像名称或摘要。

```sh
trivy image --format spdx-json --output image.spdx.json "$IMAGE"
trivy image --scanners vuln --severity HIGH,CRITICAL --exit-code 1 "$IMAGE"
```

历史站点还需在部署时轮换已暴露的密码和密钥、清理测试账号、核查既有 MySQL root 授权及受影响的文件和数据。建议在禅道后台启用登录验证码并提高口令策略。镜像部署不会清理既有数据库里的账号或处理历史入侵。先核查来源目录、扩展及持久化文件，再将历史数据迁入干净实例。
