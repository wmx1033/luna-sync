# 开发说明

## 本地验证

本项目目前使用 Python 标准库的 `unittest` 测试集：

```bash
python3 -m compileall -q app tests
python3 -m unittest discover -s tests -p 'test_core.py' -v
python3 -m unittest discover -s tests -p 'test_sync_store.py' -v
python3 -m unittest discover -s tests -p 'test_drivers.py' -v
python3 -m unittest discover -s tests -p 'test_web_app.py' -v
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
未经实机验证的设备不得复用 Luna Ultra 的认证数据或目录路径。

## 协议调试约束

仅对自有或已获授权的相机进行调试。提交日志、测试夹具和抓包摘要时，必须删除 Wi-Fi 密码、
访问令牌、序列号和媒体内容；不要将完整媒体文件提交至仓库。
