import json
from pathlib import Path
from typing import Optional

SESSION_DIR = Path(__file__).resolve().parent / "sessions"
SESSION_DIR.mkdir(exist_ok=True)


def session_path(name: str) -> Path:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in ('-', '_')).strip() or "default"
    return SESSION_DIR / f"{safe}.json"


def save_storage_state(name: str, state: dict) -> Path:
    path = session_path(name)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def load_storage_state(name: str) -> Optional[str]:
    path = session_path(name)
    return str(path) if path.exists() else None
