"""Env-driven config. ALL knobs live in .env (portability requirement)."""
import os


class Settings:
    def __init__(self) -> None:
        self.weather_source = os.getenv("WEATHER_SOURCE", "openmeteo").lower()
        self.weather_lat = os.getenv("WEATHER_LAT", "")
        self.weather_lon = os.getenv("WEATHER_LON", "")
        self.ha_url = os.getenv("HA_URL", "").rstrip("/")
        self.ha_token = os.getenv("HA_TOKEN", "")
        self.ha_weather_entity = os.getenv("HA_WEATHER_ENTITY", "weather.home")
        self.comfyui_url = os.getenv("COMFYUI_URL", "http://comfyui:8188").rstrip("/")
        self.ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
        # vision backend for AI tag-reading: "llamacpp" (homelab standard —
        # llama.cpp llama-server OpenAI-compatible endpoint) or "ollama" (legacy).
        # VISION_URL defaults to the ollama URL so either engine works out of the box.
        self.vision_engine = os.getenv("VISION_ENGINE", "ollama").lower()
        self.vision_url = os.getenv("VISION_URL", self.ollama_url).rstrip("/")
        # image-editor engine for the try-on chat: "ip2p" (resident, fast) or a
        # future "swap" engine (e.g. fluxkontext) that can't sit alongside CatVTON
        self.editor_engine = os.getenv("EDITOR_ENGINE", "ip2p").lower()
        self.max_upload_px = int(os.getenv("MAX_UPLOAD_PX", "1024"))
        self.data_dir = os.getenv("DATA_DIR", "/data")
        # optional fixed seed for reproducible try-on (None = random per request)
        seed = os.getenv("TRYON_SEED", "")
        self.tryon_seed: int | None = int(seed) if seed.isdigit() else None
        # --- DeepSeek API (stylist chat — zero VRAM, keeps GPU free for CatVTON) ---
        self.deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
        self.deepseek_base_url: str = os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ).rstrip("/")
        self.deepseek_chat_model: str = os.getenv(
            "DEEPSEEK_CHAT_MODEL", "deepseek-v4-flash"
        )


settings = Settings()
