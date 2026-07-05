# Home Intercom

Push-to-talk PWA → Home Assistant → smart speakers. Hold a button, say something, it plays on your speakers.

No ffmpeg needed — browser-native recording + pure Python PCM→WAV keeps the Docker image at 131MB.

![Screenshot](assets/screenshot-en.png)

## How it works

```
Phone PWA → Flask :8764 → Home Assistant API → speakers
                ↕
           rooms.json (config)
```

Flask handles everything: receive audio, wrap PCM as WAV, call HA play_media. No streaming — most smart speakers require complete files to play.

Auto-stop: sets `repeat=off` so speakers with repeat control stop naturally. Falls back to timer-based pause for speakers without it (`PAUSE_BUFFER` env).

## Deploy

```bash
git clone https://github.com/mdj2812/home-intercom.git
cd home-intercom

# Pre-built image from ghcr.io
export IMAGE=ghcr.io/mdj2812/home-intercom:latest
docker compose -f docker/docker-compose.example.yml up -d

# Or build locally
docker build -f docker/Dockerfile -t home-intercom:latest .
```

Images are built and pushed to ghcr.io by GitHub Actions. To upgrade:

```bash
git pull
docker compose -f docker/docker-compose.example.yml pull
docker compose -f docker/docker-compose.example.yml up -d
```

## Configuration

### Environment variables

| Variable | Description |
|----------|-------------|
| `HA_URL` | Home Assistant URL, e.g. `http://192.168.1.10:8123` |
| `HA_TOKEN` | HA long-lived access token |
| `PUBLIC_URL` | (Optional) Reverse proxy domain for HA to fetch audio |
| `AUDIO_DIR` | Audio storage path, defaults to `/data/audio` |
| `PAUSE_BUFFER` | (Optional) Fallback extra seconds before auto-pause, defaults to `0` |
| `TRUSTED_PROXY` | (Optional) Reverse proxy IP, defaults to `*` (any) |

### rooms.json

```json
{
  "living":  {"name": "Living Room", "entity": "media_player.living_room_speaker"},
  "bedroom": {"name": "Bedroom",    "entity": "media_player.bedroom_speaker"}
}
```

`entity` is the HA entity_id of your speaker. Changes take effect immediately — no restart needed.

## HTTPS

PWA recording requires HTTPS. Recommended: Caddy reverse proxy.

```Caddyfile
broadcast.your-domain.com {
    reverse_proxy 127.0.0.1:8764
}
```
