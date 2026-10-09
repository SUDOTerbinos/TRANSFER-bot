"""Persistent source-to-destination group configuration (no user sessions)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Destination:
    chat_id: int
    title: str


class DestinationStore:
    """Small JSON-backed mapping of source group IDs to destination groups."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._destinations: dict[int, Destination] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            entries = payload.get("destinations", {})
            if not isinstance(entries, dict):
                raise ValueError("'destinations' must be an object")
            for source_id, raw in entries.items():
                if not isinstance(raw, dict):
                    continue
                try:
                    chat_id = int(raw["chat_id"])
                    title = str(raw.get("title") or chat_id)
                    self._destinations[int(source_id)] = Destination(chat_id, title)
                except (KeyError, TypeError, ValueError):
                    logger.warning("Skipping invalid destination entry in %s", self.path)
        except (OSError, json.JSONDecodeError, AttributeError, ValueError):
            logger.exception("Could not read destination configuration at %s", self.path)
            self._destinations = {}

    def get(self, source_chat_id: int) -> Destination | None:
        return self._destinations.get(int(source_chat_id))

    def set(self, source_chat_id: int, destination_chat_id: int, title: str) -> None:
        self._destinations[int(source_chat_id)] = Destination(
            int(destination_chat_id), str(title)
        )
        self._save()

    def remove(self, source_chat_id: int) -> bool:
        removed = self._destinations.pop(int(source_chat_id), None) is not None
        if removed:
            self._save()
        return removed

    def _save(self) -> None:
        payload = {
            "version": 1,
            "destinations": {
                str(source_id): {"chat_id": item.chat_id, "title": item.title}
                for source_id, item in self._destinations.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(self.path.name + ".tmp")
        try:
            with temporary_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
