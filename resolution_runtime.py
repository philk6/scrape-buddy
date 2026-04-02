import hashlib
import json
import time
from pathlib import Path
from typing import Any

CACHE_PATH = Path(__file__).resolve().parent / "cache" / "resolution_cache.json"
CACHE_PATH.parent.mkdir(exist_ok=True)

DEFAULT_CACHE = {
    "queries": {},
    "provider_state": {},
}


def load_cache() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return json.loads(json.dumps(DEFAULT_CACHE))
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(DEFAULT_CACHE))


def save_cache(data: dict[str, Any]) -> None:
    CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def query_key(provider: str, query: str) -> str:
    raw = f"{provider}::{query}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def get_cached_query(provider: str, query: str) -> dict[str, Any] | None:
    cache = load_cache()
    return cache.get("queries", {}).get(query_key(provider, query))


def set_cached_query(provider: str, query: str, payload: dict[str, Any]) -> None:
    cache = load_cache()
    cache.setdefault("queries", {})[query_key(provider, query)] = payload
    save_cache(cache)


def mark_provider_status(provider: str, status: str, cooldown_seconds: int = 300) -> None:
    cache = load_cache()
    cache.setdefault("provider_state", {})[provider] = {
        "status": status,
        "until": time.time() + cooldown_seconds,
    }
    save_cache(cache)


def provider_available(provider: str) -> tuple[bool, str]:
    cache = load_cache()
    state = cache.get("provider_state", {}).get(provider)
    if not state:
        return True, "available"
    if time.time() >= float(state.get("until", 0)):
        return True, "available"
    return False, state.get("status", "cooldown")
