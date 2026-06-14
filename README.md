# Home Intercom 家庭广播系统 v1.2.2

手机 PWA 按住录音 → 松开后在小爱音箱播放。支持单房间和全部广播。

URL: `https://broadcast.home.mdj2812.top/`（Caddy 反代 → Flask :8764）
部署: NAS（Celeron N5095, Docker）

## 架构

```
PWA → Flask :8764 /convert → ffmpeg + SCP → POST n8n /intercom/play {entity, url, duration} → HA play_media → 小爱
        ↑ 动态加载 rooms.json                         ↑ 逐个房间 POST（全部广播时 4 并发）
```

- **Flask** 负责音频接收、转码、上传
- **n8n** 只负责 HA 调度：`play_media` → 状态轮询确认开始播放 → `Wait(duration)` → `Pause` 循环防止 repeat
- **rooms.json** 是单一真相源，PWA 和 Flask 都从这里读

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

**前置条件**：容器内 SCP 到 HA 需要 SSH key。确认 `~/.ssh/id_ed25519` 存在且已授权访问 HA（`192.168.99.4`）。

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

依赖：`flask`、`ffmpeg`、`openssh-client`（SCP）。

不管用哪种方式部署 Flask，都需要在 n8n 导入 `n8n_workflow.json` 并激活。

## 全部广播 vs 单房间

| | 单房间 | 全部广播 |
|------|--------|------|
| 触发 | `target=<room>` | `target=all` |
| Flask | 转码一次，POST n8n 一次 | 转码一次，POST n8n 四次（并发） |
| n8n | 1 个 workflow run | 4 个独立 workflow run，各自轮询+等待+暂停 |

## 房间映射

| target | 房间 | entity_id |
|--------|------|-----------|
| `living` | 客厅 | `media_player.xiaomi_x10a_ce5a_play_control` |
| `cinema` | 影音室 | `media_player.xiaomi_lx04_e135_play_control` |
| `study` | 书房 | `media_player.xiaomi_l17a_db94_play_control` |
| `bedroom` | 主卧 | `media_player.xiaomi_lx06_627c_play_control` |
