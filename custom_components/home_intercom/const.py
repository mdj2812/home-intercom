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
PLATFORMS: list[str] = ["number", "sensor", "binary_sensor", "switch"]
YAML_UNIQUE_ID = f"{DOMAIN}_yaml"
UI_UNIQUE_ID = DOMAIN
BUTTONS_UNIQUE_ID = f"{DOMAIN}_buttons"
KEY_BUTTON_ENTRY_ID = "button_entry_id"

# ——— Device registry (ESP32 intercom buttons, issue #40) ———
DEVICE_STORAGE_KEY = f"{DOMAIN}.devices"  # HA .storage key
DEVICE_STORAGE_VERSION = 1
DEVICE_NAME_PREFIX = "Device"  # auto-register: "Device EE:FF"
DEVICE_UPDATEABLE_FIELDS = frozenset({"name", "room", "revoked", "pending", "buttons"})
MAC_PATTERN = r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$"  # normalized uppercase form
DEVICE_REGISTRY_DEFAULT_PATH = "/data/device_registry.json"  # Docker default
ROOMS_STORE_DEFAULT = "/data/rooms.json"  # Docker writable room catalog (#72)
MAX_RECORD_SECS = 60  # recording cap delivered to ESP32 via hello/config
# ESP32 hellos every 10s while idle; 30s ≈ three missed heartbeats.
DEVICE_ONLINE_WINDOW_SECS = 30
# GPIO → room map (issue #78). Match intercom-button MAX_BUTTONS / MAX_ROOM_KEY_LEN.
GPIO_MIN = 0
GPIO_MAX = 48
MAX_DEVICE_BUTTONS = 8
MAX_BUTTON_ROOM_KEY_LEN = 32

# ——— PWA shared token (issue #54) ———
PWA_TOKEN_STORAGE_KEY = f"{DOMAIN}.pwa_token"  # HA .storage key
PWA_TOKEN_STORAGE_VERSION = 1

# ——— Custom chime (issue #66) ———
CUSTOM_CHIME_FILENAME = "custom_chime.wav"
MAX_CHIME_BYTES = 2 * 1024 * 1024  # 2 MB upload cap

# ——— Firmware OTA (GitHub release cached for LAN HTTP) ———
FIRMWARE_GITHUB_LATEST_URL = "https://api.github.com/repos/mdj2812/intercom-button/releases/latest"
FIRMWARE_GITHUB_LATEST_PAGE = "https://github.com/mdj2812/intercom-button/releases/latest"
FIRMWARE_GITHUB_DOWNLOAD_URL = (
    "https://github.com/mdj2812/intercom-button/releases/download/{tag}/{name}"
)
FIRMWARE_ASSET_BIN_RE = r"^intercom-button-.+\.bin$"
FIRMWARE_CACHE_SUBDIR = "firmware"
FIRMWARE_CACHE_BIN = "firmware.bin"
FIRMWARE_CACHE_SIG = "firmware.sig"
FIRMWARE_CACHE_META = "firmware.json"
FIRMWARE_DIR_DEFAULT = "/data/firmware"  # Docker default
# Background GitHub poll: metadata every hour; .bin download only when newer.
FIRMWARE_POLL_INTERVAL_SECS = 60 * 60

# ——— HA panel URLs ———
# Legacy underscore path still works; hyphen path satisfies HA sidebar/dashboard rules.
PANEL_PATH_LEGACY = "/home_intercom"
PANEL_PATH = "/home-intercom"
DEFAULT_CHIME_STATIC_URL = f"{PANEL_PATH}/static/pre_announce.wav"
