# 禅道 Docker 部署

本仓库维护禅道 22.7 的 Docker Compose 配置。从相邻的 [zentaopms](https://github.com/yes8080/zentaopms) 源码检出构建 PHP 8.4 FPM 和 Nginx 1.30，数据库使用 MySQL 8.4。需要 Docker Engine 和支持附加构建上下文的 Docker Compose v2。

## 选择操作指南

| 场景 | 指南 |
| --- | --- |
| 全新安装 | [默认快速启动](#默认快速启动) · [持久化、Redis 和安全增强](#全新部署持久化--redis--安全增强) |
| 已有站点更新或 Docker 重启后恢复 | [更新与恢复](docs/operations.md) |
| 调整端口、缓存或数据目录 | [部署配置](docs/configuration.md) |
| 部署公网代理与验收 | [安全增强](docs/security.md) |
| Redis 启动失败、首页 504、卷警告 | [故障排查](docs/troubleshooting.md) |

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

按上面的步骤将两个仓库克隆到同一父目录，进入 `zentaopms-docker`，再由 `.env.example` 创建 `.env`。本节适用于空数据库。已有站点请沿用原项目名、数据目录和数据库口令，参考[恢复已有 Redis 部署](docs/operations.md#docker-重启后恢复-redis-部署)。示例使用宿主机目录 `/data/zentao` 和本机端口 `8080`。

#### 1. 准备持久化目录和密码

```sh
mkdir -p /data/zentao/mysql /data/zentao/config /data/zentao/data
```

编辑 `.env`，设置 `ZENTAO_DATA_DIR=/data/zentao`。分别运行 `openssl rand -hex 24` 生成三个独立口令，填入 `MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD` 和 `ZT_REDIS_PASSWORD`。保留 `ZENTAO_BIND=127.0.0.1` 和 `ZENTAO_PORT=8080`。用 `chmod 600 .env` 限制文件权限。整个安装过程都从同一部署目录运行；如需指定 `COMPOSE_PROJECT_NAME`，从一开始就使用同一个值。

#### 2. 用普通镜像安装，保留 Redis 和持久化

将以下设置**写入现有 `.env`**。已有同名行时替换该行。

```dotenv
COMPOSE_FILE=compose.yaml:compose.persistence.yaml:compose.redis.yaml
```

```sh
unset COMPOSE_FILE
docker compose config --services
```

确认列出 `mysql`、`redis`、`php` 和 `nginx`，再启动：

```sh
docker compose up -d --build --wait &&
./scripts/check-deployment.sh http://127.0.0.1:8080 install
```

打开 `http://127.0.0.1:8080/install.php`。数据库主机填 `mysql`，端口填 `3306`，数据库名、业务账号和密码取 `.env` 的 `MYSQL_DATABASE`、`MYSQL_USER` 和 `MYSQL_PASSWORD`，表前缀保留 `zt_`。完成向导，创建管理员并登录一次。若安装页返回 404，请先核对文件集与镜像构建目标。加固镜像会在构建时移除安装页。

#### 3. 切换到加固镜像并验收

启用前请查看[安全增强的功能限制与排查](docs/troubleshooting.md#安全增强的功能限制与排查)。沿用同一项目名、数据路径和密码，把 `.env` 的 `COMPOSE_FILE` 替换为：

```dotenv
COMPOSE_FILE=compose.yaml:compose.persistence.yaml:compose.redis.yaml:compose.hardened.yaml
```

```sh
unset COMPOSE_FILE
docker compose config --services
```

再次确认四个服务均在配置中。**先构建 PHP 和 Nginx，再启动。** 普通镜像和加固镜像共用 Compose 的本地镜像名。只重启或只执行 `up -d --wait` 可能继续使用普通镜像，造成只读 PHP 容器反复尝试删除安装和升级文件。

```sh
docker compose build php nginx &&
./scripts/recover-redis-site.sh http://127.0.0.1:8080 &&
docker compose exec -T php sh -ec \
  'test ! -e /var/www/zentao/www/install.php && test ! -e /var/www/zentao/www/upgrade.php' &&
docker compose ps
```

用浏览器重新登录，确认项目或其他新建记录可读取。公开入口 `/install.php` 应返回 404；按[加固升级验收](docs/operations.md#将已有站点升级到-docker-加固版本)核对 Redis 认证读写及附件。以后从同一部署目录恢复已构建的同一套配置，可直接运行恢复脚本。

如果 Redis 无法启动，参见[排查 Redis 启动失败](docs/troubleshooting.md#排查-redis-启动失败)。如果容器已启动，但宿主机验收超时或返回 `504`，参见[排查容器已启动但首页返回 504](docs/troubleshooting.md#排查容器已启动但首页返回-504)。
