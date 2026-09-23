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
        # Qwen-Image-2.1 image-edit backend — the ONLY renderer. It needs
        # ComfyUI >= 0.37 + ComfyUI-GGUF, so it lives on its own instance
        # (202:8188). Blank = rendering is unavailable.
        self.qwen_comfyui_url = os.getenv("QWEN_COMFYUI_URL", "").rstrip("/")
        self.ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
        # vision backend for AI tag-reading: "llamacpp" (homelab standard —
        # llama.cpp llama-server OpenAI-compatible endpoint) or "ollama" (legacy).
        # VISION_URL defaults to the ollama URL so either engine works out of the box.
        self.vision_engine = os.getenv("VISION_ENGINE", "ollama").lower()
        self.vision_url = os.getenv("VISION_URL", self.ollama_url).rstrip("/")
        # image-editor engine for the legacy /api/tryon/edit endpoint: "ip2p".
        # The Outfits page "Refine this outfit" action does NOT use this — it
        # runs on the Qwen renderer above (tryon.refine_render).
        self.editor_engine = os.getenv("EDITOR_ENGINE", "ip2p").lower()
        self.max_upload_px = int(os.getenv("MAX_UPLOAD_PX", "1024"))
        self.data_dir = os.getenv("DATA_DIR", "/data")
        # optional fixed seed for reproducible try-on (None = random per request)
        seed = os.getenv("TRYON_SEED", "")
        self.tryon_seed: int | None = int(seed) if seed.isdigit() else None
        # --- try-on model backends ---
        # "qwen_edit" (Qwen-Image-2.1 image-edit) is the only renderer: the
        # CatVTON / IDM-VTON stack was removed on 2026-09-23. Still a list
        # because the dev console can queue several renders for one look.
        self.tryon_models = [
            m.strip()
            for m in os.getenv("TRYON_MODELS", "qwen_edit").split(",")
            if m.strip()
        ]
        # --- DeepSeek API (stylist chat — zero VRAM, keeps the GPU free) ---
        self.deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
        self.deepseek_base_url: str = os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ).rstrip("/")
        self.deepseek_chat_model: str = os.getenv(
            "DEEPSEEK_CHAT_MODEL", "deepseek-v4-flash"
        )
        # --- dev accounts (admin + test) ---
        # OFF by default — production must never enable them. When on, two
        # fixed dev logins exist on the login page (username, not email):
        #   admin  / <DEV_ADMIN_PASSWORD>  — can switch into ANY user and act as
        #            them (see/adjust everything, as if they were the user)
        #   test   / <DEV_TEST_PASSWORD>   — a sandbox whose data is a COPY of a
        #            real user (snapshot at copy time); changes only hit the copy
        # Defaults are the requested dev creds; override via env if you like.
        self.dev_admin_enabled: bool = os.getenv("DEV_ADMIN_ENABLED", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        self.dev_admin_login: str = os.getenv("DEV_ADMIN_LOGIN", "admin")
        self.dev_admin_password: str = os.getenv("DEV_ADMIN_PASSWORD", "Rimmer256!")
        self.dev_admin_email: str = os.getenv("DEV_ADMIN_EMAIL", "admin@dev.local")
        self.dev_test_login: str = os.getenv("DEV_TEST_LOGIN", "test")
        self.dev_test_password: str = os.getenv("DEV_TEST_PASSWORD", "Rimmer256!")
        self.dev_test_email: str = os.getenv("DEV_TEST_EMAIL", "test@dev.local")


settings = Settings()
