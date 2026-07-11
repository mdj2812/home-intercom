"""Constants for the Home Intercom custom component."""

DOMAIN = "home_intercom"

# ——— Audio constants ———
PCM_RATE = 16000  # target sample rate (Hz)
PCM_BPS = 2  # 16-bit audio = 2 bytes per sample
WAV_MAGIC = b"RIFF"
WAV_HEADER_SIZE = 44  # RIFF(12) + fmt(24) + data(8)

# ——— Config keys ———
CONF_ROOMS = "rooms"

# ——— Service names ———
SERVICE_ANNOUNCE = "announce"

# ——— Defaults ———
DEFAULT_AUDIO_DIR = "www/home_intercom_audio"
