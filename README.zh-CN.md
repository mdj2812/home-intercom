# Home Intercom 家庭广播

[![HACS](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/hacs/integration)
[![Docker](https://img.shields.io/badge/ghcr.io-mdj2812%2Fhome--intercom-blue)](https://github.com/mdj2812/home-intercom/pkgs/container/home-intercom)

手机 PWA 界面，按住说话 → 松开后通过 Home Assistant 在音箱播放。

因为用浏览器原生录音 + PCM→WAV 纯 Python 处理，所以不依赖 ffmpeg，Docker 镜像只有 131MB。

![截图](assets/screenshot.png)

## 架构

```
手机 PWA → Flask :8764 → Home Assistant API → 音箱播放
                ↕
           rooms.json（房间配置）

    ── 或者 ──

手机 PWA → HA 集成 → Home Assistant API → 音箱播放
                ↕
        configuration.yaml（YAML 配置）
```

两种部署方式：

- **HA 集成（推荐）** — 运行在 Home Assistant 内部，无需额外容器
- **Docker** — 独立 Flask 服务（传统方式，完全支持）

自动停止分三层，根据音箱能力自动选择：

1. **Music Assistant 播放器** — 原生 `play_announcement`（最快最可靠）
2. **现代播放器** — `play_media(announce=True)` + `repeat=off`（HomePod/Chromecast）
3. **普通播放器** — 播放后定时暂停（`PAUSE_BUFFER` 环境变量调整缓冲秒数）

## 推荐：使用 Music Assistant 播放器

如果音箱通过 [Music Assistant](https://music-assistant.io/) 接入，**强烈建议**使用 MA 集成创建的 `media_player` 实体。

MA 播放器支持原生 `play_announcement` 服务——播完自动停止。**无需定时暂停**（不需要 `PAUSE_BUFFER` 配置）。更可靠，延迟更低。

## 安装（HA 集成 · HACS）

[![在 Home Assistant 中打开此仓库](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mdj2812&repository=home-intercom&category=integration)

1. 在 HACS 中添加自定义仓库
2. 安装 "Home Intercom"
3. 前往 **设置 → 设备与服务 → 添加集成** → 搜索 "Home Intercom"
4. 填写表单：选择区域、媒体播放器，可选设置播报音量和暂停缓冲
5. 重复 配置 → 添加房间 来添加更多房间

也可以使用 YAML（导入后只读）：

```yaml
home_intercom:
  rooms:
    living:
      name: "客厅"
      entity_id: "media_player.living_room_speaker"
      announce_volume: 50  # 可选, 0-100
    bedroom:
      name: "主卧"
      entity_id: "media_player.bedroom_speaker"
```

YAML 房间会以独立的 "YAML" 配置条目显示。通过 UI 集成管理可编辑的房间；YAML 房间需编辑 `configuration.yaml` 并重启 HA。

4. **添加到侧边栏**：创建仪表盘 → 添加网页卡片 → URL 填 `/home_intercom`

## 安装（Docker）

```bash
git clone https://github.com/mdj2812/home-intercom.git
cd home-intercom

# 用预构建镜像
export IMAGE=ghcr.io/mdj2812/home-intercom:latest
docker compose -f docker/docker-compose.example.yml up -d

# 或者本地构建
docker build -f docker/Dockerfile -t home-intercom:latest .
```

镜像由 GitHub Actions 自动构建推到 ghcr.io。升级：

```bash
git pull
docker compose -f docker/docker-compose.example.yml pull
docker compose -f docker/docker-compose.example.yml up -d
```

### Docker 配置

#### 环境变量

| 变量 | 说明 |
|------|------|
| `HA_URL` | Home Assistant 地址，如 `http://192.168.1.10:8123` |
| `HA_TOKEN` | HA 长期访问令牌 |
| `PUBLIC_URL` | （可选）反代域名，HA 通过这个 URL 拉音频 |
| `AUDIO_DIR` | 音频存储目录，默认 `/data/audio` |
| `PAUSE_BUFFER` | （可选）后备自动暂停额外等待秒数，默认 `0` |
| `STATE_TIMEOUT` | （可选）实体状态查询超时秒数，默认 `5`（蓝牙/MA 设备增大） |
| `TRUSTED_PROXY` | （可选）反代 IP，默认 `*`（允许所有） |

#### rooms.json

```json
{
  "living":  {"name": "客厅", "entity": "media_player.living_room_speaker", "announce_volume": 50},
  "bedroom": {"name": "主卧", "entity": "media_player.bedroom_speaker"}
}
```

`entity` 填 HA 中音箱的 entity_id。改完无需重启，PWA 自动加载。

`announce_volume`（可选，0-100）仅对 Music Assistant 播放器生效。设置后 MA 会先响提示音再按指定音量播报。不填则沿用播放器当前音量。

## 前导提示音

按下对讲按钮时，门铃提示音会先于播报内容播放：

- **MA 播放器** — 通过 Music Assistant 的原生 pre-announce 流程处理
- **标准播放器** — 直接拼接到 WAV 文件中（无缝衔接，无间隙）

提示音文件位于 `/static/pre_announce.wav`，可替换为自定义 WAV（需 16kHz 单声道 16-bit）。

## HTTPS

PWA 录音需要 HTTPS。Docker 方式推荐 Caddy 反代：

```Caddyfile
broadcast.your-domain.com {
    reverse_proxy 127.0.0.1:8764
}
```

HA 集成方式下，HTTPS 由 Home Assistant 的反代统一处理。
