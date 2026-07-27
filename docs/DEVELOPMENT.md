# 开发说明

当前各里程碑的完成情况、实机验收结果与未完成项见[进度记录](PROGRESS.md)。

## 本地验证

本项目目前使用 Python 标准库的 `unittest` 测试集：

```bash
python3 -m compileall -q app tests
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

`test_web_app.py` 需要 Flask（容器内由 `python3-flask` 提供）。本地未安装时这些用例会自动跳过，
如需在本地运行，可先 `python3 -m pip install flask`。

持续集成会在推送到 `main`、`codex/**` 分支以及所有拉取请求中运行相同的测试，并额外安装 Flask，
以保证 Web 运行时与数据库的接线始终被覆盖。

## Docker / NAS 验证

复制并填写配置后构建本地镜像：

```bash
cp config.example.json config.json
mkdir -p downloads state
docker compose up -d --build
```

需要使用宿主机 NetworkManager 时：

```bash
docker compose -f docker-compose.yml -f docker-compose.networkmanager.yml up -d --build
```

部署 NAS 必须具备可由容器访问的无线网卡。Docker 配置采用 host 网络与 privileged 模式，
仅应在可信局域网使用。

## v1 → v2 配置迁移原则

M1 引入多设备 SQLite 数据库前，继续兼容现有单设备 `config.json`。首次迁移会将其创建为一个
`luna_ultra` 默认设备，并保留下载目录、状态目录及 Wi-Fi 设置；不会移动或重命名已有媒体文件。
数据库保存在 `state_dir/sync.db`，用于设备、媒体和同步任务元数据。Wi-Fi 密码不会写入 SQLite：
迁移后的默认设备只保留 `legacy-config` 引用，现有运行时仍从受限的配置来源读取密码。

## 数据持久化约定（M1）

- 每轮自动同步在相机确认可达之后才开启一条 `sync_runs` 记录，避免相机不在家时写入大量失败任务。
- 扫描结果通过一次事务批量写入 `media`。扫描会比对本地归档目录，因此它是完成状态的唯一权威：
  本地文件被删除时记录会退回 `pending`，但首次完成时间在文件仍存在时保持不变。
- 下载只有在文件写完、`.part` 原子改名之后才写入完成状态，随后把字节数计入对应任务。
- 驱动异常与下载失败写入 `sync_errors`，保留错误码与是否可重试，Web 服务不会因此退出。
- `GET /api/devices` 与 `GET /api/devices/<id>/runs` 目前是只读的；设备的增删改交由 M4 的设备管理页。
  接口不会返回 Wi-Fi 密码，也不会回显 `credential_ref`。

## 归档布局与 v1 迁移（M3）

素材统一保存为 `{下载目录}/{设备}/{年}/{月}/{日}/{存储}/{原文件名}`。设备目录取自设备 ID，
因此两台相机、两张存储卡或两天拍摄的同名文件不会互相覆盖；同路径但内容不同的文件会得到
`.conflict-N` 后缀并记录告警，绝不覆盖已有文件。无法确定拍摄日期时归入 `unknown-date/`。

v1 的 `{存储}/{文件名}` 布局会在服务启动时自动迁移一次：

- 同一文件系统内使用原子重命名，不复制数据，因此大档案也能秒级完成；跨文件系统才回退为复制。
- 中断后重启会继续未完成的部分，重复执行不会产生副作用。
- 迁移过程从不删除文件。目标已存在同名文件时只记录，不覆盖。
- 未下载完的 `.part` 会一并搬移，续传仍然有效；若最终文件已存在，残片留在原处以免截断成品。
- 每个搬移过的文件都会登记为已完成，这是防止换布局后整个档案被重新下载的关键。
- 拍摄日期依次尝试：数据库中已记录的时间、文件名中的时间戳（如 `VID_20260710_101942_062.mp4`）、
  文件修改时间。

因为文件位置由 `media.local_path` 决定，UI 使用的标识符保持不变，缩略图和转码缓存在迁移后依然有效。

## 同步引擎（M3）

`app/sync_engine.py` 负责与相机品牌无关的编排，`SyncEngine` 的职责边界是：

- 按优先级依次同步已启用设备；单块无线网卡由一把互斥锁保护，第二台设备只会显示为等待，
  不会抢占正在使用的连接。
- 下载队列就是 `media` 表的状态，不存在第二份真相；重启后 `downloading` 一律回到 `pending`，
  磁盘上的 `.part` 由下载器自动续传。
- 重试按错误类型区分：相机离线、鉴权失败、协议错误和空间不足会结束该设备本轮同步，
  单文件错误只影响自己；退避从 30 秒指数增长，上限 30 分钟，连续失败 5 次后停在 `failed` 等待人工处理。
- 取消不计入失败次数，`.part` 保留，下次直接续传。
- 只有在相机确认可达之后才开启 `sync_runs` 记录，相机不在家不会污染任务历史。

## 驱动接口约定（M2）

`app/camera_driver.py` 定义所有相机共用的契约，驱动只负责协议，不触碰队列、归档路径与 Web 状态：

- `probe()` 返回 `ProbeResult`：设备标识、可枚举存储来源、媒体数量与驱动能力，供“测试连接”使用。
- `list_media()` 返回 `RemoteMedia`；稳定 ID 由 `device_id + storage + remote_path` 组成，
  不以文件名作为跨设备唯一标识。
- `open_download(media, offset=0)` 返回 `DownloadTarget`，其中携带下载所需的请求头与续传位置。
  `downloader` 同时接受普通 URL 和 `DownloadTarget`；当驱动声明 `supports_range=False` 时，
  它会丢弃残留的 `.part` 并从头重下，而不是发出无效的 Range 请求。
- 驱动须把底层异常包装为 `DriverUnreachableError`、`DriverAuthError` 或 `DriverProtocolError`，
  它们带有错误码与可重试标记，直接决定 `sync_errors` 中的分类。
- 网络层不再假设相机固定在 `host:80`：`driver_registry.device_endpoint()` 从驱动类读取探测端口，
  Wi-Fi 模块只负责让这个地址可达。

新增驱动时在 `app/drivers/` 下实现 `CameraDriver` 并注册到 `driver_registry`，
未经实机验证的设备不得复用其他型号的认证数据或目录路径。已验证设备的协议要点、
固件版本和验收记录见 [兼容矩阵](COMPATIBILITY.md)。

`app/drivers/insta360_protocol.py` 实现了 Insta360 在 TCP 6666 上的二进制会话
（帧封装、同步握手、心跳、命令响应匹配）。其中的 protobuf 编解码只覆盖驱动实际用到的
varint 与字符串字段，因此不需要引入 protobuf 运行时依赖，容器镜像保持不变。

## 协议调试约束

仅对自有或已获授权的相机进行调试。提交日志、测试夹具和抓包摘要时，必须删除 Wi-Fi 密码、
访问令牌、序列号和媒体内容；不要将完整媒体文件提交至仓库。
