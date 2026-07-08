"""SecretStore (ADR-0006): именованные секреты, инжекция на execution-слое.

Агент оперирует только именами (api_key_ref, secrets: [...]), не значениями.
MVP-backend — файл JSON с правами 0600 вне репозитория плюс fallback на
env-переменные; шифрование файла (Fernet/KMS) — следующий backend (интерфейс
это позволяет). Значения используются для инжекции в окружение sandbox и для
redaction в trace, но никогда не попадают в контекст LLM.
"""

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path


class SecretStore(ABC):
    @abstractmethod
    def get(self, name: str) -> str | None:
        """Значение секрета по имени, либо None."""

    @abstractmethod
    def names(self) -> list[str]:
        """Известные имена секретов (для инжекции; значения не раскрываются)."""

    def values(self) -> frozenset[str]:
        """Все непустые значения — для redaction (§12)."""
        collected = {self.get(name) for name in self.names()}
        return frozenset(v for v in collected if v)


class EnvSecretStore(SecretStore):
    """Секреты из переменных окружения процесса."""

    def get(self, name: str) -> str | None:
        return os.environ.get(name) or None

    def names(self) -> list[str]:
        # Имена env не перечисляем (их тысячи); значения для redaction берём
        # только у явно запрошенных секретов через get().
        return []


class FileSecretStore(SecretStore):
    """Секреты из JSON-файла {имя: значение}; файл — в denylist и .gitignore."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._data = {str(k): str(v) for k, v in raw.items()}

    def get(self, name: str) -> str | None:
        return self._data.get(name) or None

    def names(self) -> list[str]:
        return sorted(self._data)

    def set(self, name: str, value: str) -> None:
        """Записать секрет; файл создаётся с правами 0600 (только владелец)."""
        self._data[name] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._path.chmod(0o600)


class LayeredSecretStore(SecretStore):
    """Цепочка store'ов: get — первое попадание, values — объединение."""

    def __init__(self, stores: list[SecretStore]) -> None:
        self._stores = stores

    def get(self, name: str) -> str | None:
        for store in self._stores:
            value = store.get(name)
            if value:
                return value
        return None

    def names(self) -> list[str]:
        seen: list[str] = []
        for store in self._stores:
            for name in store.names():
                if name not in seen:
                    seen.append(name)
        return seen

    def values(self) -> frozenset[str]:
        result: set[str] = set()
        for store in self._stores:
            result |= store.values()
        return frozenset(result)


def injected_env(store: SecretStore, names: list[str]) -> dict[str, str]:
    """Собрать {имя: значение} для явно выданных секретов (инжекция в sandbox)."""
    env: dict[str, str] = {}
    for name in names:
        value = store.get(name)
        if value:
            env[name] = value
    return env
