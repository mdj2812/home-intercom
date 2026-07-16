# Home Intercom

[![HACS](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/hacs/integration)
[![Docker](https://img.shields.io/badge/ghcr.io-mdj2812%2Fhome--intercom-blue)](https://github.com/mdj2812/home-intercom/pkgs/container/home-intercom)

Push-to-talk PWA → Home Assistant → smart speakers. Hold a button, say something, it plays on your speakers.

No ffmpeg needed — browser-native recording + pure Python PCM→WAV keeps the Docker image at 131MB.

![Screenshot](assets/screenshot-en.png)

## How it works

```
Phone PWA → Flask :8764 → Home Assistant API → speakers
                ↕
           rooms.json (config)

    ── or ──

Phone PWA → HA integration → Home Assistant API → speakers
                ↕
        configuration.yaml (YAML config)
```

Two deployment modes:

- **HA Integration (recommended)** — runs inside Home Assistant, no separate container
- **Docker** — standalone Flask server (legacy, still fully supported)

Auto-stop is tiered to match your speakers' capabilities:

1. **Music Assistant players** — native `play_announcement` (fastest, most reliable)
2. **Modern players** — `play_media(announce=True)` with `repeat=off` (HomePod/Chromecast)
3. **Basic players** — timer-based pause after playback (`PAUSE_BUFFER` env)

## Recommended: Use Music Assistant Players

If your speakers are integrated via [Music Assistant](https://music-assistant.io/), **strongly prefer** the `media_player` entities created by the MA integration over native speaker entities.

MA players support the native `play_announcement` service — playback stops automatically. **No timer-based pause needed** (no `PAUSE_BUFFER` config). More reliable, lower latency.

## Installation (HA Integration via HACS)

[![Open your Home Assistant instance and open the Home Intercom repository inside the HACS add-on](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mdj2812&repository=home-intercom&category=integration)

1. Add this repository as a custom repository in HACS
2. Install "Home Intercom" from HACS
3. Go to **Settings → Devices & Services → Add Integration** → search "Home Intercom"
4. Fill in the form: select an Area, pick a media player, optionally set announce volume and pause buffer
5. Repeat Configure → Add Room for each additional room

Alternatively, use YAML (read-only after import):

```yaml
home_intercom:
  rooms:
    living:
      name: "Living Room"
      entity_id: "media_player.living_room_speaker"
      announce_volume: 50  # optional, 0-100
    bedroom:
      name: "Bedroom"
      entity_id: "media_player.bedroom_speaker"
```

YAML rooms appear as a separate config entry labeled "YAML". Use the UI integration for editable management. For YAML rooms, edit `configuration.yaml` and restart HA.

4. **Add to sidebar**: create a Dashboard → add Webpage card → URL: `/home_intercom`

### Authenticated device upload

Hardware clients can upload audio through Home Assistant's standard authentication:

```http
POST /api/home_intercom/device/record?target=living
Authorization: Bearer <HA_LONG_LIVED_ACCESS_TOKEN>
Content-Type: audio/wav
```

Create a Long-Lived Access Token from the Home Assistant user profile and send it over HTTPS.
Unlike the PWA's short-lived shared token, this endpoint continues to work after Home Assistant
restarts. The existing `/api/home_intercom/record` endpoint remains available to the bundled PWA.

## Installation (Docker)

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

### Docker Configuration

#### Environment variables

| Variable | Description |
|----------|-------------|
| `HA_URL` | Home Assistant URL, e.g. `http://192.168.1.10:8123` |
| `HA_TOKEN` | HA long-lived access token |
| `PUBLIC_URL` | (Optional) Reverse proxy domain for HA to fetch audio |
| `AUDIO_DIR` | Audio storage path, defaults to `/data/audio` |
| `PAUSE_BUFFER` | (Optional) Fallback extra seconds before auto-pause, defaults to `0` |
| `STATE_TIMEOUT` | (Optional) Seconds to wait for entity state queries, defaults to `5` (increase for Bluetooth/MA devices) |
| `TRUSTED_PROXY` | (Optional) Reverse proxy IP, defaults to `*` (any) |

#### rooms.json

```json
{
  "living":  {"name": "Living Room", "entity": "media_player.living_room_speaker", "announce_volume": 50},
  "bedroom": {"name": "Bedroom",    "entity": "media_player.bedroom_speaker"}
}
```

`entity` is the HA entity_id of your speaker. Changes take effect immediately — no restart needed.

`announce_volume` (optional, 0-100) overrides the announcement volume for Music Assistant players only. When set, MA will play a chime then announce at the specified volume. Omit the field to use the player's current volume.

## Pre-announce chime

When you press the intercom button, a doorbell chime plays before your announcement. The chime is:

- **MA players** — handled natively via Music Assistant's pre-announce flow
- **Standard players** — prepended directly into the WAV file (seamless, no gap)

The chime file is served from `/static/pre_announce.wav` and can be replaced with your own WAV (16 kHz mono 16-bit required).

## HTTPS

PWA recording requires HTTPS. For Docker, recommended: Caddy reverse proxy.

```Caddyfile
broadcast.your-domain.com {
    reverse_proxy 127.0.0.1:8764
}
```

For the HA integration, HTTPS is handled by your Home Assistant reverse proxy.
