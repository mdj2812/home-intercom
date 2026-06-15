# Changelog

## v1.5.1 (2026-06-15)

- **修复** — ffmpeg 失败时 `os.unlink()` 文件不存在导致 500 crash，加 `os.path.exists()` 守卫

## v1.5.0 (2026-06-15)

- **去掉 n8n** — Flask 直接调 HA REST API，不再依赖 n8n webhook
- **HAClient** — 抽取 `ha_client.py`，封装 HA 状态查询、服务调用、播放+自动暂停
- **auto-pause** — 后台线程轮询确认播放开始 → 等音频时长 → pause + 5 次重试，防止小爱 repeat
- **HA_URL** — 废弃 `HA_HOST` + `HA_SCHEME`，改为单一 `HA_URL` 变量，`urlparse()` 解析
- **SELF_URL 移除** — 废代码清理，URL 自动检测
- **docker-compose.example** — 同步更新 HA_URL 配置

## v1.4.4 (2026-06-15)

- **修复** — `stopRecording()` 中停止 MediaStream tracks 后漏设 `mediaRecorder = null`，导致第二次录音失败（400）

## v1.4.3 (2026-06-15)

- **WAV 直通** — ESP32 硬件按键发来的 PCM WAV 跳过 ffmpeg，直接 serve；用 `wave` 模块解析头，不硬编码 offset
- **音频处理重构** — 抽出 `_handle_wav_passthrough()` / `_handle_webm_convert()`，`convert()` 路由瘦身
- **常量声明** — `WAV_MAGIC`、`TMP_PREFIX`、`FFMPEG_SR`/`BPS`/`BYTERATE` 模块级常量，消除魔数
- **麦克风释放时机** — 从 `sendAudio()` 移到 `stopRecording()`，录音结束立即释放，不等网络请求
- **ffmpeg 异常处理** — try/except 仅包裹 ffmpeg 分支，失败时 `os.unlink()` 清理残留文件

## v1.4.2 (2026-06-14)

- **Flask → waitress** — 生产级 WSGI 服务器，支持多线程并发，去掉开发服务器警告
- **waitress trusted_proxy 配置** — 正确传递 `X-Forwarded-Proto` header，Caddy 反代时 URL 正确拼 `https://`
- **麦克风释放** — 发送完成后释放 MediaStream，浏览器不再显示麦克风占用
- **日志修复** — `PYTHONUNBUFFERED=1` + logging 配置，waitress 下日志正常输出

## v1.4.1 (2026-06-14)

- **去掉 SELF_URL** — 改用 `request.host_url` + `X-Forwarded-Proto` 自动获取，Caddy 反代时正确拼 `https://`
- **去掉 compose ports** — host 网络模式无需端口映射
- **README 清理** — 去掉环境变量默认值

## v1.5.0 (2026-06-14)

- **去掉 n8n** — Flask 直接调 HA REST API `media_player/play_media`，不再依赖 n8n webhook
- 删除 `N8N_HOOK` 环境变量，`_ha_call()` 直接 POST HA 服务端点
- 全部广播：Flask 并行 fire-and-forget 各房间，不再通过 n8n 串行调度

## v1.4.0 (2026-06-14)

- **去掉 SCP/SSH** — Flask 本地 serve 音频，HA 通过 HTTP 直接拉取，不再依赖 SSH key
- 新增 `SELF_URL`、`AUDIO_DIR` 环境变量
- Dockerfile 移除 openssh-client，compose 移除 SSH key 挂载

## v1.3.2 (2026-06-14)

- **全部广播卡片横排布局** — 左文右按钮，Grid 布局，space-around 均匀分布
- **标题优化** — 缩小字号、左对齐、减少顶部间距

## v1.3.1 (2026-06-14)

- **UI 大改版** — Apple Home 风格卡片网格，圆形按压说话按钮，录音/发送/已发送状态动效
- **绿色主题统一** — 录音→发送→已发送，按钮、边框全程绿色，橙色转圈标识发送中
- **音箱在线指示** — 房间名右侧绿点/灰点，每 30s 轮询 HA 查询小爱音箱状态
- **修复** — 发送中动画不显示（CSS selector）、状态文字抖动（固定高度）、按钮溢出裁剪

## v1.2.2 (2026-06-14)

- **NAS 部署** — 确认 Celeron N5095 转码足够快（10s 音频 < 10ms），迁移到 NAS
- **超时调整** — ffmpeg 15→60s, SCP 10→30s，兼容低性能设备
- **CI** — 建立 Gitea Actions，`.docker-image` 变更自动 build & push

## v1.2.1 (2026-06-14)

- **端口改为 8764** — 避免与 NAS 上其他容器冲突

## v1.2.0 (2026-06-14)

- **PWA 图标** — 广播主题图标（SVG + 4 尺寸 PNG），支持添加到主屏幕
- **Caddy 反向代理** — `https://broadcast.home.mdj2812.top/` HTTPS 访问，满足 PWA getUserMedia 要求
- **项目目录整理** — `src/` 源码、`docker/` 容器配置、`assets/` 素材、`n8n/` workflow 备份
- **静态文件路由** — `/static/<path>` catch-all 替代 5 个独立路由
- **镜像版本管理** — `docker/.docker-image` 单一真相源

## v1.1.0 (2026-06-14)

- **全部广播** — 顶部金色「全部」按钮，一次录音同时发送到所有房间
- **n8n 状态轮询** — 播放后轮询 `state=="playing"` 确认音箱真正开始播，再倒计时 Wait(duration)；Pause 后循环确认停止，解决 repeat:all 导致的重播
- **固定文件名** — 每房间一个固定文件 `intercom_<room>.wav`，新录音覆盖旧，不会堆积
- **rooms.json 外部化** — 添加/修改房间只需改 JSON，PWA 按钮自动生成，无需改代码
- **PWA 触感优化** — 按钮间距、高度自适应、「按住录音松开发送」提示
