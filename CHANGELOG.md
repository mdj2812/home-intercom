# Changelog

## v1.6.3 (2026-07-10)

- **Room status & friendly names** — `/rooms/status` now returns `{status, friendly_name}` per room, device names displayed on room cards
- **Offline button disable** — PTT buttons greyed out when speaker is unavailable or lacks `play_media` support
- **State timeout** — `STATE_TIMEOUT` env var (default 5s) for slow Bluetooth/MA entities; query timeouts are handled gracefully
- **Preview fallback** — local dev without `HA_TOKEN` now shows mock room data instead of a blank grid
- **i18n dropdown** — language selector moved to footer with dropdown UX, emojis removed from i18n strings
- **Room card redesign** — room name + device name on left, icon on right; horizontal broadcast card layout
- **PTT icon** — unified 9-cell grid structure for CSS theming flexibility
- **Footer** — version number + language selector in footer, avoiding header layout jumps
- **CSS refactor** — grid → flexbox, responsive sizing, stylelint compliance
- **Tests** — 96 pytest tests, 88% coverage

## v1.6.2 (2026-07-08)

- **Pre-announce chime** — doorbell sound prepended to announcements via WAV-level concatenation
- **Two-tier chime strategy** — MA players use native `use_pre_announce`, standard players get chime baked into audio
- **Configurable announcement volume** — `announce_volume` per room in `rooms.json`, volume slider + mute/unmute in web UI (MA players only)
- **Disable unavailable speakers** — greyed out button with tooltip, frontend guard on start recording
- **`_concat_wavs()`** — WAV concatenation helper with format mismatch fallback (copies original on mismatch)
- **Chime file** — Universfield Clear Bell Chime (2s, +12dB), served from `/static/pre_announce.wav`
- **Stylelint** — restored `extends: stylelint-config-standard`, Python test uses `npm install -g` to match CI
- **Tests** — 88 pytest tests, 94% coverage

## v1.6.1 (2026-07-07)

- **Chinese README** — added `README.zh-CN.md` with localized UI screenshot
- **English README** — switched primary language to English, de-branded (generic `media_player`, not Xiaomi-specific)
- **Public release cleanup** — sanitized internal IPs, domains, and device IDs
- **AUDIO_DIR fix** — moved `os.makedirs()` after config validation to avoid empty directory residue
- **GitHub Actions CI** — quality (lint + test + coverage≥85%) and build-docker workflows
- **Docker image** — split `docker/.docker-image` into version-only, CI variables managed separately
- **Three-tier auto-stop** — MA `play_announcement` → modern `repeat_set(off)` → basic timer fallback
- **repeat_set smart stop** — HomePod/Chromecast players stop naturally via `SUPPORT_REPEAT_SET`, no timer needed
- **PAUSE_BUFFER configurable** — env var to adjust timer buffer duration, fixes Xiaomi early cutoff
- **Entity attribute caching** — `_get_entity_info()` with double-checked locking + success-only cache, prevents transient errors from permanently downgrading speaker capabilities
- **Refactor** — `state()` as single entry point, `_play_media()` and `_entity_attrs()` extracted, eliminates duplicate API calls
- **Music Assistant guide** — README section recommending MA players for native `play_announcement` (no timer needed)
- **Tests** — 74 pytest tests, 94% coverage

## v1.6.0 (2026-06-28)

- **去掉 ffmpeg** — Docker 镜像 579→131MB，纯 Python PCM→WAV，无外部依赖
- **PCM 录音** — MediaRecorder opus 采集 → decodeAudioData 解码 → OfflineAudioContext 重采样 16kHz
- **`/record` 端点** — 替代 `/convert`，支持裸 PCM（PWA）和完整 WAV（ESP32 按键）双输入
- **PUBLIC_URL** — 反代场景（Caddy）支持，HA 通过域名获取音频
- **setPointerCapture** — PPT 按键可靠性修复，避免手指晃动导致录音截断
- **review** — 常量化（WAV_MAGIC/PCM_BPS/WAV_HEADER_SIZE），函数封装，ruff 格式化
- **测试** — 59 个 pytest 测试，93% 覆盖率

## v1.5.2 (2026-06-15)

- **i18n** — PWA 中英文切换（zh-CN / en），右上角按钮，localStorage 持久化
- **i18n 房间名** — 语言切换时房间名同步翻译，使用 `rooms.json` 的 `name_en` 字段
- **测试** — 57 个 pytest 测试，93% 覆盖率，含 HTML/JS 质量检查
- **lint** — ruff 静态检查 + format，CI 自动执行
- **CI quality workflow** — push/PR 自动 lint + test + coverage≥85%

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
- **Caddy 反向代理** — `https://broadcast.example.com/` HTTPS 访问，满足 PWA getUserMedia 要求
- **项目目录整理** — `src/` 源码、`docker/` 容器配置、`assets/` 素材、`n8n/` workflow 备份
- **静态文件路由** — `/static/<path>` catch-all 替代 5 个独立路由
- **镜像版本管理** — `docker/.docker-image` 单一真相源

## v1.1.0 (2026-06-14)

- **全部广播** — 顶部金色「全部」按钮，一次录音同时发送到所有房间
- **n8n 状态轮询** — 播放后轮询 `state=="playing"` 确认音箱真正开始播，再倒计时 Wait(duration)；Pause 后循环确认停止，解决 repeat:all 导致的重播
- **固定文件名** — 每房间一个固定文件 `intercom_<room>.wav`，新录音覆盖旧，不会堆积
- **rooms.json 外部化** — 添加/修改房间只需改 JSON，PWA 按钮自动生成，无需改代码
- **PWA 触感优化** — 按钮间距、高度自适应、「按住录音松开发送」提示
