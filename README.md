# Home Intercom 家庭广播系统

## 架构

```
手机 PWA → n8n webhook → Flask 转换 (ffmpeg + SCP) → n8n HA node → 小爱音箱
```

## 文件

| 文件 | 说明 |
|------|------|
| `intercom_server.py` | Flask 后端 — 接收音频 → ffmpeg 转 WAV → SCP 到 HA |
| `intercom.html` | 手机 PWA 对讲页面 |
| `n8n_workflow.json` | n8n 工作流导出（Webhook → HTTP Request → Home Assistant） |

## 部署

1. Flask: `python3 intercom_server.py` (HTTP port 8765)
2. PWA: 通过 Flask 的 `/` 路由提供
3. n8n: 导入 `n8n_workflow.json`，配置 HA 凭据

## 房间映射

| target | 房间 | 音箱 |
|--------|------|------|
| `living` | 客厅 | Smart Display 10 |
| `cinema` | 影音室 | Mi Smart Clock 4inch |
| `study` | 书房 | Sound Pro |
