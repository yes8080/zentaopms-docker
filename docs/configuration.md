# 部署配置

[返回安装指南](../README.md)

## 修改监听地址或端口

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

加固部署的默认备份另外存放在项目命名卷 `app-backup`。跨主机迁移时应单独导出并核验该卷。

空子目录会在首次启动时初始化。后续更新、重启或停止时继续传入 `-f compose.persistence.yaml`，使用缓存时可以再叠加对应配置文件。例如：

```sh
docker compose -f compose.yaml -f compose.persistence.yaml -f compose.redis.yaml up -d --build --wait
```

已有站点切换前，先备份并把原命名卷中的数据迁入新目录。从官方一体化镜像迁移时，应导出并导入数据库、迁移附件，并核对新容器的数据库连接设置。部署后如需更换宿主机路径，先备份并迁移三个子目录，停止容器并删除对应的三个 `host-*` Docker 卷定义，再用新路径重新启动。[官方 Docker 部署文档](https://www.zentao.net/book/zentaopms/docker-1111.html)
