# 禅道 Docker 部署

本仓库维护禅道 22.7 的 Docker Compose 配置。从相邻的 [zentaopms](https://github.com/yes8080/zentaopms) 源码检出构建 PHP 8.4 FPM 和 Nginx 1.28，数据库使用 MySQL 8.4。需要 Docker Engine 和支持附加构建上下文的 Docker Compose v2。

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

## 可选缓存

默认部署不安装 APCu 或 Redis PHP 扩展，也不启动 Redis 服务。需要缓存时，选择下面一种配置。后续执行同一部署的 Compose 命令时，继续传入对应的配置文件。

**APCu** 适用于单个 PHP 容器，缓存保存在该容器的共享内存中。

```sh
docker compose -f compose.yaml -f compose.apcu.yaml up -d --build --wait
```

**Redis** 启用 PHP Redis 扩展和 Compose 网络内的 Redis 服务。Redis 不发布宿主机端口，缓存数据仅保存在内存中。

```sh
docker compose -f compose.yaml -f compose.redis.yaml up -d --build --wait
```

Redis 镜像同时支持 PHP 内置序列化器和 igbinary。默认使用 `php`，也可以在 `.env` 中设置 `ZT_REDIS_SERIALIZER=igbinary`。升级旧镜像时，先用上面的 `--build` 命令重建。

禅道数据库中已保存的缓存设置优先于容器配置。已有站点启用或切换缓存时，在「后台 → 系统 → 缓存设置」确认驱动与序列化方式。使用 Redis 时，主机填 `redis`，端口填 `6379`，数据库选 `0`，用户名和密码留空。切换序列化方式时清理禅道缓存，避免读取旧格式数据。使用 APCu 时选择共享模式。命名空间可填应用数据库名。

恢复默认缓存配置前，先在禅道后台关闭缓存，再省略缓存配置文件并添加 `--remove-orphans`。若正在使用宿主机目录挂载，继续传入 `-f compose.persistence.yaml`。下面的命令适用于默认命名卷部署，也会清理先前启用的 Redis 容器。

```sh
docker compose up -d --build --wait --remove-orphans
```

## 挂载到宿主机目录

默认使用 Docker 命名卷。也可以像官方镜像一样指定一个宿主机目录集中存放持久化数据。这里由独立的 MySQL、PHP 和 Nginx 容器分别挂载所需的子目录。

在 `.env` 中设置 `ZENTAO_DATA_DIR=/srv/zentao`，使用 Docker 主机上的绝对路径。先创建三个子目录，再启动服务：

```sh
mkdir -p /srv/zentao/mysql /srv/zentao/config /srv/zentao/data
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

## 源码更新和数据

更新相邻源码检出后，在本仓库按所选配置再次运行启动命令。Compose 命名卷保存数据库、安装生成的配置和上传文件。MySQL 初始化变量仅在空数据库卷上生效。迁移已有安装时，请先备份数据库、应用配置和上传文件，再导入对应卷。

## 安全增强

先按前文完成安装或数据库升级，再启用生产运行配置。首次安装和升级应在仅运维可访问的环境中进行。后续 Compose 命令继续传入安全增强配置文件，使用宿主机挂载或缓存时同时传入相应配置文件。

```sh
docker compose -f compose.yaml -f compose.hardened.yaml build --pull
docker compose -f compose.yaml -f compose.hardened.yaml up -d --no-build --wait
```

- PHP 和 Nginx 以只读根文件系统运行。禅道配置卷只读。PHP 仅为应用临时目录、系统临时目录和应用数据卷保留写权限。缓存、会话和本地日志随 PHP 容器重建清空。禅道内部仍可生成并读取临时 PHP 缓存，临时目录位于 Web 根目录之外。
- Nginx 只将受支持的入口交给 PHP-FPM，拒绝访问 Adminer、隐藏文件、安装与升级入口以及数据目录中的服务端脚本。上传目录设置浏览器沙箱，Nginx 对数据卷保持只读。
- 禅道按解析后的路由阻止在线编辑、二次开发、运行时插件变更和接收客户端 SQL 的图表预览接口。此配置会禁用对应的 BI 预览。需要的扩展由运维审查后随镜像发布。
- 常规登录提交按来源 IP 限流并返回 HTTP 429，所有 PHP 请求另有通用速率限制，禅道保留自身的账号失败次数和锁定机制。反向代理后部署时，应由运维按实际拓扑配置可信代理，否则限流按代理地址计数。
- MySQL 与 Web 文件隔离。业务账号仅使用应用库的权限，新数据库仅创建本地 root 账号。安全增强模式关闭 general log、服务端文件导入导出和客户端本地文件导入。PHP 镜像构建要求 PHP 8.4.27 或更新版本。

历史站点还需在部署时轮换已暴露的密码和密钥、清理测试账号、核查既有 MySQL root 授权及受影响的文件和数据。建议在禅道后台启用登录验证码并提高口令策略。镜像部署不会清理既有数据库里的账号或处理历史入侵。先核查来源目录、扩展及持久化文件，再将历史数据迁入干净实例。
