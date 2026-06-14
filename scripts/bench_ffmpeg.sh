#!/bin/bash
# ffmpeg 性能测试 — 在 NAS 上运行
# 测试 5s / 10s / 20s 音频转换耗时

TEST_DIR=/tmp/intercom-bench
mkdir -p "$TEST_DIR"

echo "=== NAS 性能: ffmpeg webm→wav ==="
echo "CPU: $(grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2 | xargs)"
echo "Arch: $(uname -m)"
echo ""

for DUR in 5 10 20; do
    WEBM="$TEST_DIR/test_${DUR}s.webm"
    WAV="$TEST_DIR/test_${DUR}s.wav"

    # 生成测试音频 (sine tone + silence)
    ffmpeg -y -f lavfi -i "sine=frequency=440:duration=${DUR}" \
           -f lavfi -i "anullsrc=r=16000:cl=mono" \
           -filter_complex "[0:a]volume=0.3[a1];[a1][1:a]amix=inputs=2:duration=first,volume=2[out]" \
           -map "[out]" -ac 1 -ar 16000 \
           -c:a libopus -b:a 24k \
           "$WEBM" 2>/dev/null

    SIZE=$(stat -c%s "$WEBM" 2>/dev/null)
    echo -n "  ${DUR}s 录音 (${SIZE}B) → 转码耗时: "

    START=$(date +%s%N)
    ffmpeg -y -i "$WEBM" -acodec pcm_s16le -ac 1 -ar 16000 "$WAV" 2>/dev/null
    END=$(date +%s%N)

    ELAPSED=$(( (END - START) / 1000000 ))
    echo "${ELAPSED}ms"

    rm -f "$WEBM" "$WAV"
done

rm -rf "$TEST_DIR"
echo ""
echo "结论: 如果 10s 音频转码 > 3000ms，NAS 确实不适合。"
