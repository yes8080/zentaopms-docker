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

禅道数据库中已保存的缓存设置优先于容器配置。已有站点启用或切换缓存时，在「后台 → 系统 → 缓存设置」确认驱动与所选配置一致。使用 Redis 时，主机填 `redis`，端口填 `6379`，数据库选 `0`，序列化方式选 `php`，用户名和密码留空。使用 APCu 时选择共享模式。命名空间可填应用数据库名。

恢复默认部署前，先在禅道后台关闭缓存，再运行以下命令。该命令也会清理先前启用的 Redis 容器。

```sh
docker compose up -d --build --wait --remove-orphans
```

## 源码更新和数据

更新相邻源码检出后，在本仓库按所选配置再次运行启动命令。Compose 命名卷保存数据库、安装生成的配置和上传文件。MySQL 初始化变量仅在空数据库卷上生效。迁移已有安装时，请先备份数据库、应用配置和上传文件，再导入对应卷。
