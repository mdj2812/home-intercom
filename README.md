# Home Intercom 家庭广播系统 v1.0

手机 PWA 按住录音 → 松开后在小爱音箱播放。

## 架构

```
手机 PWA → n8n webhook → Flask /convert (8765) → ffmpeg 转 WAV → SCP 到 HA → play_media → 小爱音箱
```

n8n 负责编排调度，Flask 负责音频转换和 HA API 调用。

## 文件

| 文件 | 说明 |
|------|------|
| `intercom_server.py` | Flask 后端 — 提供 PWA 页面 + `/convert` 端点（接收音频 → ffmpeg → SCP → HA play_media） |
| `intercom.html` | 手机 PWA 对讲页面 — 4 个按住录音按钮，POST 到 n8n webhook |
| `n8n_workflow.json` | n8n 工作流（Webhook → HTTP Request → Flask /convert） |

## 部署

1. **Flask**: `python3 intercom_server.py`（HTTP :8765，同时提供 PWA 和 API）
2. **n8n**: 导入 `n8n_workflow.json`，配置 HA 凭据
3. **PWA**: 访问 `http://<host>:8765/`

依赖：`flask`、`ffmpeg`、`sshpass`、HA API token、n8n。

## 房间映射

| target | 房间 | 音箱 | entity_id |
|--------|------|------|-----------|
| `living` | 客厅 | Smart Display 10 | `media_player.xiaoai_wifispeaker_x10a` |
| `cinema` | 影音室 | Mi Smart Clock 4inch | `media_player.xiaoai_wifispeaker_lx04` |
| `study` | 书房 | Sound Pro | `media_player.xiaoai_wifispeaker_l17a` |
| `bedroom` | 主卧 | AI Speaker Pro | `media_player.xiaoai_wifispeaker_lx06` |
