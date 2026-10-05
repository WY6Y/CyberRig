"""Memory channel store — JSON persistence. No built-in defaults; these are
personal snapshots of the operator's own favorite spots, not generic examples."""

import json
from cyberrig.memories.model import MemoryChannel
from cyberrig.settings import _config_dir


def _memories_file():
    return _config_dir() / "memories.json"


def load_memories() -> list[MemoryChannel]:
    f = _memories_file()
    if not f.exists():
        return []
    try:
        with open(f) as fh:
            data = json.load(fh)
        return [MemoryChannel.from_dict(d) for d in data]
    except Exception:
        return []


def save_memories(memories: list[MemoryChannel]):
    f = _memories_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "w") as fh:
        json.dump([m.to_dict() for m in memories], fh, indent=2)
