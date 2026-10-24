import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

_DEFAULTS = {
    "shop_domain": "",
    "admin_api_token": "",
    "api_version": "2025-10",
    "mode": "mock",  # mock | live
    "openai_api_key": "",
    "brand": "Nimbus Outfitters",
    "site_url": "",
    "category_prompts": [
        "best waterproof travel backpack for rainy weather",
        "rain jacket for city commuting",
        "durable weekender bag under $200",
    ],
}


def load_config() -> dict:
    cfg = dict(_DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            cfg.update({k: v for k, v in stored.items() if k in cfg})
        except (OSError, ValueError):
            pass
    return cfg


def save_config(updates: dict) -> dict:
    cfg = load_config()
    cfg.update({k: v for k, v in updates.items() if k in cfg})
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg
