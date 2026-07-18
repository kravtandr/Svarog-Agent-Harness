"""Персистентная история ввода чата (стрелки вверх/вниз, как в shell).

Файл — построчный, в user-state директории `~/.svarog/` (конвенция
config.loader.USER_CONFIG_PATH): вне workspace агента, чтобы не попадать
под `assert_workspace_isolated` и в коммиты.
"""

from pathlib import Path

HISTORY_LIMIT = 1000  # строк в файле; старые обрезаются при загрузке


def default_history_path() -> Path:
    return Path("~/.svarog/chat_history").expanduser()


class InputHistory:
    """История отправленных сообщений с курсором для навигации стрелками."""

    def __init__(self, path: Path | None = None, *, limit: int = HISTORY_LIMIT) -> None:
        self._path = path or default_history_path()
        self._limit = limit
        self._entries: list[str] = []
        self._cursor: int | None = None  # None — вне навигации (свежий ввод)
        self._draft = ""  # незаконченный ввод, к которому вернёт «вниз»
        self._load()

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self._entries = [line for line in raw.splitlines() if line.strip()][-self._limit :]

    @property
    def entries(self) -> list[str]:
        return list(self._entries)

    def append(self, text: str) -> None:
        """Дописать отправленное сообщение (пустые и повтор последнего — нет)."""
        text = text.strip()
        if not text or (self._entries and self._entries[-1] == text):
            self.reset_cursor()
            return
        self._entries.append(text)
        self._entries = self._entries[-self._limit :]
        self.reset_cursor()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("\n".join(self._entries) + "\n", encoding="utf-8")
        except OSError:
            # История — удобство, не данные: сбой записи не роняет чат.
            pass

    def reset_cursor(self) -> None:
        self._cursor = None
        self._draft = ""

    def prev(self, current: str = "") -> str | None:
        """Шаг назад по истории; при первом шаге запоминаем черновик ввода."""
        if not self._entries:
            return None
        if self._cursor is None:
            self._draft = current
            self._cursor = len(self._entries) - 1
        elif self._cursor > 0:
            self._cursor -= 1
        return self._entries[self._cursor]

    def next(self) -> str | None:
        """Шаг вперёд; после последней записи возвращает сохранённый черновик."""
        if self._cursor is None:
            return None
        if self._cursor < len(self._entries) - 1:
            self._cursor += 1
            return self._entries[self._cursor]
        draft = self._draft
        self.reset_cursor()
        return draft
