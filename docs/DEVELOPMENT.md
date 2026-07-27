# 开发说明

## 本地验证

本项目目前使用 Python 标准库的 `unittest` 测试集：

```bash
python3 -m compileall -q app tests
python3 -m unittest discover -s tests -p 'test_core.py' -v
```

持续集成会在推送到 `main`、`codex/**` 分支以及所有拉取请求中运行相同的测试。

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

## 协议调试约束

仅对自有或已获授权的相机进行调试。提交日志、测试夹具和抓包摘要时，必须删除 Wi-Fi 密码、
访问令牌、序列号和媒体内容；不要将完整媒体文件提交至仓库。
