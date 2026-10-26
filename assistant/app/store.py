"""Small JSON store on the /data volume: timers, alarms, reminders, memories and lists."""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("store")

_DEFAULT: dict[str, Any] = {
    "timers": [],      # {id, label, due, duration}
    "alarms": [],      # {id, label, time "HH:MM", repeat "once|daily|weekdays|weekends", next_due, snoozes}
    "reminders": [],   # {id, text, due}
    "memories": [],    # {id, text, created}
    "lists": {"shopping": [], "to-do": []},  # name -> [{id, text, done}]
    "camera_spots": [],  # {id, name, pan, tilt}  saved camera positions
    "wifi_pending": [],  # {id, ssid, password, created}  waiting to be sent to the board
    "wifi_saved": [],    # {id, ssid}  networks the board says it has saved
}


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config.DATA_DIR / "assistant.json")
        self._lock = threading.RLock()
        self.data: dict[str, Any] = json.loads(json.dumps(_DEFAULT))
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                loaded = json.loads(self.path.read_text())
                for key, value in _DEFAULT.items():
                    self.data[key] = loaded.get(key, json.loads(json.dumps(value)))
            except FileNotFoundError:
                pass
            except Exception as exc:
                log.error("Could not read %s (%s); starting empty", self.path, exc)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            os.replace(tmp, self.path)

    # convenience -------------------------------------------------------------
    def add(self, kind: str, item: dict) -> dict:
        with self._lock:
            item.setdefault("id", uuid.uuid4().hex[:8])
            self.data[kind].append(item)
            self.save()
            return item

    def remove(self, kind: str, ids: set[str]) -> int:
        with self._lock:
            before = len(self.data[kind])
            self.data[kind] = [i for i in self.data[kind] if i["id"] not in ids]
            self.save()
            return before - len(self.data[kind])

    def items(self, kind: str) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self.data[kind]]

    def update(self, kind: str, item_id: str, **changes) -> None:
        with self._lock:
            for i in self.data[kind]:
                if i["id"] == item_id:
                    i.update(changes)
            self.save()

    # lists -------------------------------------------------------------------
    def list_names(self) -> list[str]:
        with self._lock:
            return list(self.data["lists"].keys())

    def list_items(self, name: str) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self.data["lists"].get(name, [])]

    def list_add(self, name: str, texts: list[str]) -> list[str]:
        with self._lock:
            lst = self.data["lists"].setdefault(name, [])
            existing = {i["text"].lower() for i in lst}
            added = []
            for t in texts:
                if t.lower() not in existing:
                    lst.append({"id": uuid.uuid4().hex[:8], "text": t, "done": False})
                    existing.add(t.lower())
                    added.append(t)
            self.save()
            return added

    def list_remove(self, name: str, texts: list[str] | None = None, ids: set[str] | None = None) -> list[str]:
        with self._lock:
            lst = self.data["lists"].get(name, [])
            removed, keep = [], []
            wanted = [t.lower() for t in (texts or [])]
            for i in lst:
                hit = (ids and i["id"] in ids) or any(
                    w == i["text"].lower() or w in i["text"].lower() or i["text"].lower() in w
                    for w in wanted)
                (removed if hit else keep).append(i)
            self.data["lists"][name] = keep
            self.save()
            return [i["text"] for i in removed]

    def list_clear(self, name: str) -> int:
        with self._lock:
            n = len(self.data["lists"].get(name, []))
            self.data["lists"][name] = []
            self.save()
            return n

    def list_toggle(self, name: str, item_id: str) -> None:
        with self._lock:
            for i in self.data["lists"].get(name, []):
                if i["id"] == item_id:
                    i["done"] = not i.get("done")
            self.save()

    def list_delete(self, name: str) -> None:
        with self._lock:
            self.data["lists"].pop(name, None)
            self.save()
