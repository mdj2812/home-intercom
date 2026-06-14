# Home Intercom 家庭广播系统 v1.2.1

手机 PWA 按住录音 → 松开后在小爱音箱播放。支持单房间和全部广播。

URL: `https://broadcast.home.mdj2812.top/`（Caddy 反代 → Flask :8764）

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
│   └── docker-compose.yml
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
└── README.md
```

## v1.2.1 更新 (2026-06-14)

- **端口改为 8764** — 避免与 NAS 上其他容器冲突

## v1.2.0 更新 (2026-06-14)

- **PWA 图标** — 广播主题图标（SVG + 4 尺寸 PNG），支持添加到主屏幕
- **Caddy 反向代理** — `https://broadcast.home.mdj2812.top/` HTTPS 访问，满足 PWA getUserMedia 要求
- **项目目录整理** — `src/` 源码、`docker/` 容器配置、`assets/` 素材、`n8n/` workflow 备份
- **静态文件路由** — `/static/<path>` catch-all 替代 5 个独立路由

## v1.1.0 更新 (2026-06-14)

- **全部广播** — 顶部金色「全部」按钮，一次录音同时发送到所有房间
- **n8n 状态轮询** — 播放后轮询 `state=="playing"` 确认音箱真正开始播，再倒计时 Wait(duration)；Pause 后循环确认停止，解决 repeat:all 导致的重播
- **固定文件名** — 每房间一个固定文件 `intercom_<room>.wav`，新录音覆盖旧，不会堆积
- **rooms.json 外部化** — 添加/修改房间只需改 JSON，PWA 按钮自动生成，无需改代码
- **PWA 触感优化** — 按钮间距、高度自适应、「按住录音松开发送」提示

## 部署

### Docker（推荐）

```bash
cd /path/to/home-intercom
docker compose -f docker/docker-compose.yml up -d
```

首次会在 `http://<host>:8764/` 启动，`rooms.json` volume 挂载，改完 `docker compose -f docker/docker-compose.yml restart` 即可。

镜像：`registry.home.mdj2812.top/home-lab/home-intercom:latest`

版本号由 `docker/.docker-image` 维护（单一真相源），`docker/.env` 供 compose 使用。升级版本时同步更新这两个文件。

**前置条件**：容器内 SCP 到 HA 需要 SSH key。确认 `~/.ssh/id_ed25519` 存在且已授权访问 HA（`192.168.99.4`）。

### Caddy 反代

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
