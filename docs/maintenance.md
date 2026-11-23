# 用 zentaoctl 维护禅道

从部署仓库根目录执行 `./scripts/zentaoctl`。以下示例适用于有本地 Docker 权限的 Linux 或 WSL 终端，需要 Python 3.9+、Git、curl 和 Docker Compose v2。构建时，同级目录 `../zentaopms` 应是要发布的业务源码。已有手工部署从[接管现有站点](#接管现有站点)开始。

## 新装和加固

为每个站点取固定项目名并分配独立环境文件。下面使用 `zentao`、回环端口 8080 和 Redis。先创建只有当前用户能读取的配置目录，再运行安装。新站使用独立的 Docker 命名卷。

```sh
install -d -m 700 "$HOME/.config/zentao"
./scripts/zentaoctl install --project zentao \
  --env-file "$HOME/.config/zentao/zentao.env" \
  --storage volume --cache redis --port 8080 --dry-run
```

核对项目和端口后，移除 `--dry-run` 执行同一条命令。要把数据放在宿主机目录，将 `--storage volume` 改为 `--storage bind --data-dir /绝对路径/zentao`，选用这个实例独占的空路径。缓存可选 `none`、`apcu` 或 `redis`。工具将缺失的数据库口令及所需的 Redis 口令写入该实例的环境文件。

在部署主机浏览器中打开命令输出的安装向导地址。数据库主机填 `mysql`、端口填 `3306`，数据库名和用户默认为 `zentao`，业务密码取环境文件中的 `MYSQL_PASSWORD`。完成向导，创建管理员并登录。需要加固时执行下列命令。加固会替换 PHP 和 Nginx，并关闭安装与升级入口，请预留维护窗口。

```sh
./scripts/zentaoctl harden --project zentao --dry-run
./scripts/zentaoctl harden --project zentao
./scripts/zentaoctl status --project zentao --verify
```

重新登录，打开一条已知业务记录和一份历史附件。Redis 部署还应核对禅道后台保存的缓存设置。加固模式对插件、附件及部分开发功能的影响见[功能限制](troubleshooting.md#安全增强的功能限制与排查)。

## 接管现有站点

先列出项目，再用原部署目录、项目名和实际环境文件检查。示例中的 `$PWD/.env` 应替换为原站使用的文件。

```sh
./scripts/zentaoctl status
./scripts/zentaoctl status --project zentao --env-file "$PWD/.env"
./scripts/zentaoctl adopt --project zentao --env-file "$PWD/.env" --dry-run
./scripts/zentaoctl adopt --project zentao --env-file "$PWD/.env"
```

接管会核对容器、镜像、数据来源及站点入口，然后将登记记录写入部署目录的 `.zentaoctl/zentao/`。后续命令可从记录读取环境文件路径。保留这个私有目录，其中包含配置校验密钥、已验收的配置和生成的覆盖文件。

遇到阻断时，先核对**原项目名、完整且有序的 Compose 文件、MySQL/配置/附件卷及其实际宿主机路径**。启用了 Redis 的两个项目可能都有四个服务，却使用完全不同的数据库。旧容器上的标签可帮助找回原来的配置位置。恢复所需覆盖文件和环境文件后，再重新预检。[历史挂载与孤立 Redis 排查](operations.md#docker-重启后恢复-redis-部署)

## 日常操作

| 场景 | 命令 |
| --- | --- |
| 查看状态、镜像和数据来源 | `./scripts/zentaoctl status --project zentao` |
| 检查 HTTP 入口与 PHP 依赖 | `./scripts/zentaoctl status --project zentao --verify` |
| Docker 重启后恢复原实例 | `./scripts/zentaoctl recover --project zentao` |
| 维护停机或重启 | `./scripts/zentaoctl stop --project zentao` 或 `./scripts/zentaoctl restart --project zentao` |
| 改用回环端口 8081 | `./scripts/zentaoctl configure --project zentao --bind 127.0.0.1 --port 8081` |

变更命令可先加 `--dry-run`。实例健康且配置一致时，`recover` 会验收入口。容器缺失时，它先核实原有卷和镜像，再补建服务。看到未完成操作时，请保留原目录和数据，查看 `status`，按照提示使用原参数续跑。

## 配置和版本更新

`configure` 支持监听地址、端口和缓存变更。更换缓存需要独立备份目录和维护窗口；提前核对禅道后台缓存驱动与序列化设置。数据目录搬迁需要单独迁移并校验数据。可用 `./scripts/zentaoctl configure --help` 查看选项。

更新前，在两个仓库各自准备目标提交及干净工作树，记录其提交号。使用原项目名和一个尚不存在的备份目录，先预演，再去掉 `--dry-run` 执行：

```sh
./scripts/zentaoctl update --project zentao \
  --app-ref "$(git -C ../zentaopms rev-parse HEAD)" \
  --deployment-ref "$(git rev-parse HEAD)" \
  --backup-dir "$HOME/zentao-backups/before-update" --dry-run
```

请先创建 `$HOME/zentao-backups` 父目录。`--app-ref` 和 `--deployment-ref` 校验当前检出版本。遇到需要升级数据库的版本，先在恢复副本演练，并安排受限入口执行禅道升级。[已有站点升级步骤](operations.md#将已有站点升级到-docker-加固版本)

## 冷备份与离线恢复

备份需要运行中的 Nginx、PHP 和 MySQL。工具在维护窗口内停止 Web 入口和 PHP，正常关闭 MySQL，归档完成后重新启动原容器。站点在此期间不可用，耗时随数据库、附件和复制速度变化。适用服务组合为 MySQL、PHP、Nginx，可加 Redis。

```sh
install -d -m 700 "$HOME/zentao-backups"
./scripts/zentaoctl backup --project zentao --output "$HOME/zentao-backups/before-release"
./scripts/zentaoctl status --project zentao --verify
```

目标应是站点数据目录以外、父目录已存在的全新目录。归档包含物理数据库、配置、附件、禅道内置备份，以及部署环境文件和恢复所需的配置。**其中含有凭据**，离机保存时请使用加密存储。清单记录镜像 ID，异机恢复还需另行保存并导入对应的 Docker 镜像。归档完成后，应做一次独立恢复演练。

```sh
./scripts/zentaoctl restore --project zentao \
  --from-backup "$HOME/zentao-backups/before-release" \
  --to-project zentao-review --port 18080
```

`--project` 是备份清单中的源项目，`--to-project` 是尚未使用的新项目。工具校验各文件摘要和本机镜像 ID，将四份数据写入独立的新卷，并生成私有 Compose 文件。**恢复完成时副本站点处于离线状态。** 确认副本数据库和缓存指向、后台任务、通知和外部集成后，使用工具输出的启动命令。用副本回环地址登录，验证原业务记录、历史附件和所需内置备份，再安排人工切换入口。仅使用可信来源的备份。

## 重装、卸载与删除数据

- **重装**使用 `reinstall` 指定新项目、新环境文件、新回环端口和旧站备份目录，默认使用全新的命名卷。先用 `./scripts/zentaoctl reinstall --help` 查看参数。备份完成后按新装流程完成向导，原站继续使用原入口。业务数据恢复演练请使用 `restore`。
- **卸载**使用 `./scripts/zentaoctl uninstall --project zentao`。它移除当前项目的容器和网络，保留持久卷及登记。旧实例可能仍在容器可写层或临时目录中存有备份，卸载前先完成冷备和回读验收。
- **永久删除数据**先卸载，再执行 `./scripts/zentaoctl purge-data --project zentao --dry-run` 查看精确卷名。确认备份和恢复结果后，使用 `--confirm-project` 确认项目，并为每个待删卷分别传入 `--confirm-volume`。可选范围是登记的、项目专有且无容器引用的本地数据库、配置及附件命名卷。独立备份卷保留。使用宿主机目录时请按下节逐项预检。

### 清理宿主机数据目录

以下流程只适用于已卸载的实例，且使用本仓库的本地卷驱动把三个目录绑定到 Docker。清理前结束其他部署、备份和宿主机写操作。准备好位于数据根目录以外、来自同一实例的完整冷备份。以数据根 `/data/zentao` 为例，下面只预演删除三个已登记的子目录和对应绑定卷定义：

```sh
./scripts/zentaoctl purge-data --project zentao --confirm-project zentao \
  --from-backup "$HOME/zentao-backups/before-release" \
  --confirm-dir /data/zentao/mysql \
  --confirm-dir /data/zentao/config \
  --confirm-dir /data/zentao/data --dry-run
```

预检会比较引擎和目录身份、原数据库 UUID、卷设备、备份清单及其他容器和卷的引用，遍历目录检查权限、符号链接、特殊文件和嵌套挂载。查看列出的**每一个绝对路径**后，才移除 `--dry-run` 执行。工具保留 `/data/zentao` 父目录、未选的同级目录和独立备份。按多批清理时将 `mysql` 留到最后，其他角色仍需用原数据库 UUID 核实身份。

远端引擎、Docker Desktop/WSL 文件系统身份不一致、路径与其他挂载重叠，或者执行账号无权管理目录时，命令会停止。清理中断后请保留错误中给出的临时目录，核对现场和备份再处理。

## 在测试机复跑回归

在部署仓库根目录执行下列命令。需要 Python、Docker Compose、可用的 Docker 引擎及测试镜像。Docker 回归使用随机项目名和临时数据卷，覆盖配置漂移、恢复、安装续跑和加固切换。轻量容器模拟网页向导的业务响应。实际版本发布时，还需用该版本禅道登录并回读业务数据和历史附件。

```sh
ZENTAOCTL_TEST_DOCKER=1 python3 -m unittest discover -s tests -v
```
