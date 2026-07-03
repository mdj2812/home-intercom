## 全部广播 vs 单房间

| | 单房间 | 全部广播 |
|------|--------|------|
| 触发 | `target=<room>` | `target=all` |
| Flask | PCM→WAV 一次，调 HA API 一次 | PCM→WAV 一次，调 HA API N 次 |
| 播放 | HA play_media → 音箱 | 各房间独立 HA play_media |
