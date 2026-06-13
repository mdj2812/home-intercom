# Home Intercom 家庭广播系统 v1.0

手机 PWA 按住录音 → 松开后在小爱音箱播放。

## 架构

```
手机 PWA → Flask (8765) → ffmpeg 转 WAV → SCP 到 HA → play_media → 小爱音箱
```

纯 Flask 直连，无 n8n 依赖。

## 文件

| 文件 | 说明 |
|------|------|
| `intercom_server.py` | Flask 后端 — 提供 PWA 页面 + 接收音频 → ffmpeg 转 WAV → SCP 到 HA → 调用 play_media |
| `intercom.html` | 手机 PWA 对讲页面 — 4 个按住录音按钮 |
| `n8n_workflow.json` | n8n 工作流导出（历史参考，v1.0 已废弃 n8n 链路） |

## 部署

```bash
python3 intercom_server.py   # HTTP :8765，同时提供 PWA 页面和 API
```

访问 `http://<host>:8765/` 打开对讲页面。

依赖：`flask`、`ffmpeg`、`sshpass`、HA API token。

## 房间映射

| target | 房间 | 音箱 | entity_id |
|--------|------|------|-----------|
| `living` | 客厅 | Smart Display 10 | `media_player.xiaoai_wifispeaker_x10a` |
| `cinema` | 影音室 | Mi Smart Clock 4inch | `media_player.xiaoai_wifispeaker_lx04` |
| `study` | 书房 | Sound Pro | `media_player.xiaoai_wifispeaker_l17a` |
| `bedroom` | 主卧 | AI Speaker Pro | `media_player.xiaoai_wifispeaker_lx06` |
