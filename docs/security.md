# 安全增强

[返回安装指南](../README.md)

先按前文完成安装或数据库升级，再启用生产运行配置。首次安装和升级应在仅运维可访问的环境中进行。后续 Compose 命令继续传入安全增强配置文件，使用宿主机挂载或缓存时同时传入相应配置文件。

```sh
docker compose -f compose.yaml -f compose.hardened.yaml build --pull
docker compose -f compose.yaml -f compose.hardened.yaml up -d --no-build --wait
```

- PHP 和 Nginx 以只读根文件系统运行。禅道配置卷只读。PHP 仅为应用临时目录、系统临时目录、应用数据卷和备份卷保留写权限。默认应用备份写入独立的项目卷 `app-backup`。缓存、会话和本地日志随 PHP 容器重建清空，维护后用户需要重新登录。禅道内部仍可生成并读取临时 PHP 缓存，临时目录位于 Web 根目录之外。
- Nginx 只将受支持的入口交给 PHP-FPM，拒绝访问 Adminer、隐藏文件、安装与升级入口以及数据目录中的服务端脚本。浏览器请求已有附件直链时，PHP 使用禅道的登录会话、公司和文件归属验证访问权限，通过后由 Nginx 只读发送文件。保留图片显示和视频 Range 请求，响应使用浏览器沙箱及 private, no-store。
- 禅道按解析后的路由阻止在线编辑、二次开发、运行时插件变更和接收客户端 SQL 的图表预览接口。此配置会禁用对应的 BI 预览。需要的扩展由运维审查后随镜像发布。
- 网页登录和 API v1 令牌签发按客户端地址限流，超限返回 HTTP 429。普通 PHP 请求及附件读取使用各自的配额。禅道原有的账号失败次数和锁定策略继续生效。
- MySQL 与 Web 文件隔离。业务账号仅使用应用库的权限，新数据库仅创建本地 root 账号。安全增强模式关闭 general log、服务端文件导入导出和客户端本地文件导入。PHP 镜像构建要求 PHP 8.4.27 或更新版本。
- Nginx 只加入 Web 网络，MySQL 和 Redis 只加入内部数据网络，PHP 加入两者。PHP 以非 root 用户运行，PHP、Nginx 和 Redis 收敛 capabilities，并设置内存和进程数上限。
- Nginx 访问日志记录路由类别，PHP-FPM 记录方法、状态和耗时。旧 API 访问记录及数据库错误请求关联记录仅保留白名单内的入口名。容器标准输出使用轮转日志。需要跨容器重建保留的日志应交给部署环境的集中采集服务。禅道保存在数据库里的错误日志随数据库卷保留。

### 公网代理与部署验收

同机反向代理保持后端发布在 `127.0.0.1`；容器化代理可改用专用共享网络。最外层网关限制合法 Host，清除客户端自带的 `Client-IP`，并以可信连接的客户端地址覆盖 `X-Real-IP`。在加固 Nginx 挂载只读文件 `/etc/nginx/zentao-proxy/edge.conf`，仅信任实际连接该服务的最后一跳。先通过正常请求核对宿主端口转发后的对端地址。以下地址为文档示例。

```nginx
set_real_ip_from 192.0.2.10;
set $zentao_public_scheme https;
```

保持本地 HTTP 时可省略该文件。在网关开放之前，从两个授权客户端分别登录，核对 IP 归属、网页登录及 API 使用情况。外层网关、错误日志和集中采集服务也要保护请求参数、敏感路径及 Referer。

发布前带上实际项目名、环境文件和全部 Compose 配置验证资源限额。例如使用基础和加固组合时运行：

```sh
./scripts/check-runtime-limits.sh -f compose.yaml -f compose.hardened.yaml
```

该命令核对容器配置及内核实际执行的内存和 PID 限额。若报告 rootless Docker 缺少 cgroup 驱动，在部署主机配置 cgroup v2 和 systemd 委派后复查。该检查独立于应用启动。

### 已有数据、附件和表格

- 原有加固部署的 `tmp/backup` 可能位于 tmpfs。**首次切换前**在运行中的 PHP 容器导出需要保留的备份。使用与部署相同的 `dc` 命令组合，例如 `(umask 077 && dc exec -T php tar -C /var/www/zentao/tmp/backup -cf - . > zentao-backup.tar)`，核对归档后再重建。新版本默认目录链接到持久卷 `/var/lib/zentao/backup`。需要导入旧备份时运行 `dc exec -T php tar -C /var/lib/zentao/backup --skip-old-files -xf - < zentao-backup.tar`。使用后台自定义备份目录时应单独持久化该路径。依照统一恢复点将数据库、配置、附件备份到受限的异地存储，并做恢复演练。
- 用一个私有项目的成员和一个非成员验收历史任务附件、文档缩略图、富文本图片和 MP4 拖动；同时验收裁剪头像、上传者预览和正常下载。未绑定的附件仅上传者能读取，已发布头像对已登录用户可见。尚无明确对象权限的第三方模块应核对其原生文件入口。外部邮件或 Webhook 客户端若只拿静态 URL 且没有有效会话，将无法读取私有内容。现有大型附件库应按实际规模测缩略图加载时间，检查 `file.pathname` 查询的执行计划。
- 加固 PHP 镜像使用锁定版本的 PhpSpreadsheet 1.30.8，在构建时通过禅道封装写入并回读 XLS、XLSX。部署后再用本单位的历史模板验证导入、导出及公式。源代码版本或依赖布局发生变化时，需重新核对部署镜像的兼容适配。
