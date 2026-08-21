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
        self.max_upload_px = int(os.getenv("MAX_UPLOAD_PX", "1024"))
        self.data_dir = os.getenv("DATA_DIR", "/data")


settings = Settings()
