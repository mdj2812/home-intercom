# Home Intercom 家庭广播系统 v1.1.0

手机 PWA 按住录音 → 松开后在小爱音箱播放。支持单房间和全部广播。

## 架构

```
PWA → Flask :8765 /convert → ffmpeg + SCP → POST n8n /intercom/play {entity, url, duration} → HA play_media → 小爱
        ↑ 动态加载 rooms.json                         ↑ 逐个房间 POST（全部广播时 4 并发）
```

- **Flask** 负责音频接收、转码、上传
- **n8n** 只负责 HA 调度：`play_media` → 状态轮询确认开始播放 → `Wait(duration)` → `Pause` 循环防止 repeat
- **rooms.json** 是单一真相源，PWA 和 Flask 都从这里读

## v1.1.0 更新 (2026-06-14)

- **全部广播** — 顶部金色「全部」按钮，一次录音同时发送到所有房间
- **n8n 状态轮询** — 播放后轮询 `state=="playing"` 确认音箱真正开始播，再倒计时 Wait(duration)；Pause 后循环确认停止，解决 repeat:all 导致的重播
- **固定文件名** — 每房间一个固定文件 `intercom_<room>.wav`，新录音覆盖旧，不会堆积
- **rooms.json 外部化** — 添加/修改房间只需改 JSON，PWA 按钮自动生成，无需改代码
- **PWA 触感优化** — 按钮间距、高度自适应、「按住录音松开发送」提示

## 文件

| 文件 | 说明 |
|------|------|
| `intercom_server.py` | Flask 后端 — PWA 页面 + `/convert` 端点 + `/rooms.json` 端点 |
| `intercom.html` | 手机 PWA — 动态加载 rooms.json 生成按钮，全部广播按钮固定置顶 |
| `rooms.json` | 房间映射配置 — 单文件即可增删房间 |
| `n8n_workflow.json` | n8n v3 — Webhook → HA 播放 → 状态轮询 → Wait → Pause 循环 |

## 部署

1. **Flask**: `python3 intercom_server.py`（HTTP :8765）
2. **n8n**: 导入 `n8n_workflow.json`，激活
3. **PWA**: `http://<host>:8765/`

依赖：`flask`、`ffmpeg`、`openssh-client`（SCP）、n8n。

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
