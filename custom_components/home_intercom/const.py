"""Constants for the Home Intercom custom component."""

DOMAIN = "home_intercom"

# ——— Audio constants ———
PCM_RATE = 16000  # target sample rate (Hz)
PCM_BPS = 2  # 16-bit audio = 2 bytes per sample
WAV_MAGIC = b"RIFF"
WAV_HEADER_SIZE = 44  # RIFF(12) + fmt(24) + data(8)

# ——— Config keys ———
CONF_ROOMS = "rooms"
CONF_AREA_ID = "area_id"
CONF_ANNOUNCE_VOLUME = "announce_volume"
CONF_PAUSE_BUFFER = "pause_buffer"

# ——— Service names ———
SERVICE_ANNOUNCE = "announce"

# ——— Defaults ———
AUDIO_SUBDIR = "home_intercom_audio"
WWW_DIR = "www"

# ——— Config entry ———
PLATFORMS: list[str] = ["number", "sensor", "binary_sensor"]
YAML_UNIQUE_ID = f"{DOMAIN}_yaml"
UI_UNIQUE_ID = DOMAIN

# ——— Device registry (ESP32 intercom buttons, issue #40) ———
DEVICE_STORAGE_KEY = f"{DOMAIN}.devices"  # HA .storage key
DEVICE_STORAGE_VERSION = 1
DEVICE_NAME_PREFIX = "Device"  # auto-register: "Device EE:FF"
DEVICE_UPDATEABLE_FIELDS = frozenset({"name", "room", "revoked"})
MAC_PATTERN = r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$"  # normalized uppercase form
DEVICE_REGISTRY_DEFAULT_PATH = "/data/device_registry.json"  # Docker default
MAX_RECORD_SECS = 60  # recording cap delivered to ESP32 via hello/config

# ——— PWA shared token (issue #54) ———
PWA_TOKEN_STORAGE_KEY = f"{DOMAIN}.pwa_token"  # HA .storage key
PWA_TOKEN_STORAGE_VERSION = 1
