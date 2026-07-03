# Home Intercom 家庭广播系统

手机 PWA 卡片式界面，按住房间卡片录音 → 松开后在小爱音箱播放。支持单房间和全部广播，实时显示音箱在线状态。

URL: `https://broadcast.example.com/`（Caddy 反代 → Flask :8764）
部署: NAS（Celeron N5095, Docker）

## 架构

```
PWA → Flask :8764 /record → PCM→WAV → 本地 /audio/ 目录
        ↑ 动态加载 rooms.json       ↓ HA API play_media
        ↕ /rooms/status → HA API    ↓ 小爱直接 HTTP 拉 Flask 音频
```

- **Flask** 负责一切：音频接收、PCM→WAV、本地 serve、音箱状态查询、调 HA API 播放
- **HA 直接从 Flask HTTP 拉音频**
- **无需 n8n、无需 SSH** —— Flask 拿到 HA_TOKEN 直接调 REST API

## UI 特性

- 卡片网格布局，每房间独立圆形按压说话按钮
- 录音：波纹扩散 + 声波跳动，按钮变绿
- 发送中：绿色光环绕按钮旋转
- 已发送：绿色大对勾
- 房间名右侧指示灯：🟢 音箱在线 / ⚫ 离线（HA API 每 30s 轮询）

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `HA_URL` | Home Assistant API 地址（含协议端口） | — |
| `HA_TOKEN` | HA 长期访问令牌 | — |
| `AUDIO_DIR` | 音频文件存储目录 | `/data/audio` |

## 目录结构

```
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .docker-image       # 单一版本真相源
│   └── .env.example
├── src/
│   ├── intercom_server.py    # Flask 后端
│   ├── ha_client.py          # HA API 客户端
│   ├── intercom.html         # PWA 前端
│   ├── rooms.json            # 房间配置
│   ├── requirements.txt      # Python 依赖
│   └── static/               # PWA 图标 + manifest
├── assets/
│   └── icon.svg              # 图标源文件
├── scripts/
│   └── bench_ffmpeg.sh       # NAS 性能测试
├── .gitea/workflows/
│   └── build-docker.yml      # CI：.docker-image 变更 → 自动 build & push
├── CHANGELOG.md
└── README.md
```

## 部署

### Docker（推荐，运行在 NAS）

```bash
cd /path/to/home-intercom
docker compose -f docker/docker-compose.yml up -d
```

镜像：`registry.example.com/your-project/home-intercom:latest`

版本号由 `docker/.docker-image` 维护（单一真相源），`docker/.env` 供 compose 使用。升级时：

```bash
git pull
docker compose -f docker/docker-compose.yml pull
docker compose -f docker/docker-compose.yml up -d
```

**前置条件**：Flask 直接调 HA API，确保 `HA_TOKEN` 配置正确。HA 通过 HTTP 拉音频，URL 由 Flask 自动检测。**无需 SSH key、无需 n8n**。

### 性能验证

```bash
bash scripts/bench_ffmpeg.sh
```

Celeron N5095 实测 10s 音频转码 < 10ms，绰绰有余。

### Caddy 反代

NAS 上 Caddy：

```Caddyfile
broadcast.example.com {
    reverse_proxy 127.0.0.1:8764
}
```

HTTPS 是 PWA 录音（getUserMedia）的浏览器强制要求。

### 裸机

```bash
cd src
pip install -r requirements.txt
python3 intercom_server.py  # HTTP :8764
```

依赖：`flask`、`waitress`。

## 全部广播 vs 单房间

| | 单房间 | 全部广播 |
||------|--------|------|
|| 触发 | `target=<room>` | `target=all` |
|| Flask | PCM→WAV 一次，调 HA API 一次 | PCM→WAV 一次，调 HA API N 次 |
|| 播放 | HA play_media → 音箱 | 各房间独立 HA play_media |
