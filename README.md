# Home Intercom 家庭广播系统 v1.0

手机 PWA 按住录音 → 松开后在小爱音箱播放。

## 架构

```
PWA → Flask :8765 /convert → ffmpeg + SCP → POST n8n /intercom/play {entity, url} → HA play_media → 小爱
```

- **Flask** 负责音频接收、转码、上传
- **n8n** 只负责 HA 调度（接收 URL → 播放），不处理二进制

## 文件

| 文件 | 说明 |
|------|------|
| `intercom_server.py` | Flask 后端 — PWA 页面 + `/convert` 端点（音频→ffmpeg→SCP→回调 n8n） |
| `intercom.html` | 手机 PWA — 4 个按住录音按钮 |
| `n8n_workflow.json` | n8n 播放 workflow（Webhook `/intercom/play` → HA play_media） |

## 部署

1. **Flask**: `python3 intercom_server.py`（HTTP :8765）
2. **n8n**: 导入 `n8n_workflow.json`，激活
3. **PWA**: `http://<host>:8765/`

依赖：`flask`、`ffmpeg`、`sshpass`、n8n。

## 房间映射

| target | 房间 | entity_id |
|--------|------|-----------|
| `living` | 客厅 | `media_player.xiaomi_x10a_ce5a_play_control` |
| `cinema` | 影音室 | `media_player.xiaomi_lx04_e135_play_control` |
| `study` | 书房 | `media_player.xiaomi_l17a_db94_play_control` |
| `bedroom` | 主卧 | `media_player.xiaomi_lx06_627c_play_control` |
