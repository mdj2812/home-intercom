# Home Intercom 家庭广播系统

手机 PWA 卡片式界面，按住房间卡片录音 → 松开后在小爱音箱播放。支持单房间和全部广播，实时显示音箱在线状态。

URL: `https://broadcast.home.mdj2812.top/`（Caddy 反代 → Flask :8764）
部署: NAS（Celeron N5095, Docker）

## 架构

```
PWA → Flask :8764 /convert → ffmpeg → 本地 /audio/ 目录
        ↑ 动态加载 rooms.json       ↓ POST n8n {entity, url, duration}
        ↕ /rooms/status → HA API    ↓ HA play_media → 小爱（直接 HTTP 拉 Flask 音频）
```

- **Flask** 负责音频接收、转码、本地存储和 serve、音箱状态查询
- **HA 直接从 Flask HTTP 拉音频**，不再依赖 SCP/SSH
- **n8n** 只负责 HA 调度：`play_media` → 状态轮询确认开始播放 → `Wait(duration)` → `Pause` 循环防止 repeat
- **rooms.json** 是单一真相源，PWA 和 Flask 都从这里读

## UI 特性

- 卡片网格布局，每房间独立圆形按压说话按钮
- 录音：波纹扩散 + 声波跳动，按钮变绿
- 发送中：绿色光环绕按钮旋转
- 已发送：绿色大对勾
- 房间名右侧指示灯：🟢 音箱在线 / ⚫ 离线（HA API 每 30s 轮询）

## 环境变量

| 变量 | 说明 |
|------|------|
| `HA_HOST` | Home Assistant 地址 |
| `N8N_HOOK` | n8n webhook URL |
| `HA_TOKEN` | HA 长期访问令牌（状态查询用） |
| `SELF_URL` | Flask 自身可访问 URL（HA 拉音频用） |
| `AUDIO_DIR` | 音频文件存储目录 |

## 目录结构

```
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .docker-image       # 单一版本真相源
│   └── .env.example
├── src/
│   ├── intercom_server.py    # Flask 后端
│   ├── intercom.html         # PWA 前端
│   ├── rooms.json            # 房间配置
│   ├── requirements.txt      # Python 依赖
│   └── static/               # PWA 图标 + manifest
├── assets/
│   └── icon.svg              # 图标源文件
├── n8n/
│   └── n8n_workflow.json     # n8n workflow 备份
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

镜像：`registry.home.mdj2812.top/home-lab/home-intercom:latest`

版本号由 `docker/.docker-image` 维护（单一真相源），`docker/.env` 供 compose 使用。升级时：

```bash
git pull
docker compose -f docker/docker-compose.yml pull
docker compose -f docker/docker-compose.yml up -d
```

**前置条件**：无需 SSH key。Flask 直接 serve 音频，HA 通过 HTTP 拉取。确保 `SELF_URL` 配置为 HA 能访问到的地址。

### 性能验证

```bash
bash scripts/bench_ffmpeg.sh
```

Celeron N5095 实测 10s 音频转码 < 10ms，绰绰有余。

### Caddy 反代

NAS 上 Caddy：

```Caddyfile
broadcast.home.mdj2812.top {
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

依赖：`flask`、`ffmpeg`。

不管用哪种方式部署 Flask，都需要在 n8n 导入 `n8n_workflow.json` 并激活。

## 全部广播 vs 单房间

| | 单房间 | 全部广播 |
|------|--------|------|
| 触发 | `target=<room>` | `target=all` |
| Flask | 转码一次，POST n8n 一次 | 转码一次，POST n8n 四次（并发） |
| n8n | 1 个 workflow run | 4 个独立 workflow run，各自轮询+等待+暂停 |
