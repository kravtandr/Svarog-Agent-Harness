"""Редактируемая часть конфигурации для веб-интерфейса.

Форма собирается **из схемы**: тип, пределы и допустимые значения каждого
поля берутся из той же модели Pydantic, по которой Сварог читает
`svarog.yaml`. Значит форма не может предложить настройку, которую конфиг
отвергнет, — и наоборот, новое ограничение в схеме сразу действует в форме.

Редактируются не все 24 секции, а перечисленные ниже поля: остальное либо
задаётся при `svarog init` (провайдеры, пути), либо не имеет смысла менять
на ходу. Список — единственное место, где решается, что показывать.
"""

import difflib
import re
from typing import Any, Literal, get_args, get_origin

import yaml
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from svarog_harness.config.schema import SvarogConfig

FieldKind = Literal["bool", "int", "float", "str", "enum"]


class ConfigFieldView(BaseModel):
    """Одно поле формы; всё, кроме подписи, выведено из схемы."""

    path: str
    label: str
    help: str = ""
    kind: FieldKind
    value: Any = None
    choices: list[str] = []
    minimum: float | None = None
    maximum: float | None = None


class ConfigSectionView(BaseModel):
    key: str
    title: str
    fields: list[ConfigFieldView]


class ConfigView(BaseModel):
    """Текущие значения и то, куда они будут записаны."""

    path: str
    sections: list[ConfigSectionView]


class ConfigUpdateRequest(BaseModel):
    """Изменения формы: путь поля → новое значение."""

    values: dict[str, Any]


class DiffLine(BaseModel):
    kind: Literal["same", "add", "del"]
    text: str


class ConfigDiffView(BaseModel):
    path: str
    lines: list[DiffLine]
    changes: int


# Подписи и пояснения — единственное, чего нет в схеме. Порядок здесь же
# определяет порядок разделов и полей на экране.
_LAYOUT: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    (
        "policies",
        "Политики и автономия",
        [
            (
                "runtime.autonomy",
                "Уровень автономии",
                "Как поступать с действиями среднего риска: записью в файлы, "
                "установкой пакетов, сетевыми запросами.",
            ),
            (
                "runtime.max_iterations",
                "Максимум шагов в одном запуске",
                "Агент остановится и спросит, продолжать ли, когда упрётся в предел.",
            ),
            (
                "git.require_approval_for_push",
                "Спрашивать перед push в удалённый репозиторий",
                "Действует независимо от автономии: push отнесён к высокому риску.",
            ),
            (
                "git.secret_scan_before_commit",
                "Искать секреты перед коммитом",
                "Отключение игнорируется для репозиториев с публичным remote.",
            ),
        ],
    ),
    (
        "limits",
        "Пределы запуска",
        [
            (
                "runtime.max_context_tokens",
                "Потолок контекста, токенов",
                "При достижении run приостанавливается со сбросом контекста.",
            ),
            ("runtime.max_tokens_per_run", "Потолок токенов на запуск", ""),
            ("runtime.max_cost_usd_per_run", "Потолок стоимости запуска, $", ""),
        ],
    ),
    (
        "executor",
        "Исполнитель и песочница",
        [
            (
                "executor.type",
                "Исполнитель",
                "Нативный цикл или внешний агент. Внешнему нужна секция "
                "executor.external — задаётся при svarog init.",
            ),
            ("sandbox.type", "Песочница", "docker — изоляция; local-trusted — без неё."),
            ("sandbox.timeout_sec", "Таймаут команды, секунд", ""),
        ],
    ),
    (
        "models",
        "Модели",
        [
            (
                "models.default",
                "Провайдер по умолчанию",
                "Имя из секции models.providers. Сами провайдеры задаются при svarog init.",
            ),
        ],
    ),
]


def _field_info(path: str) -> FieldInfo:
    """FieldInfo поля по пути `секция.поле` — источник типа и ограничений."""
    section_name, _, field_name = path.partition(".")
    section = SvarogConfig.model_fields[section_name]
    section_model = section.annotation
    assert isinstance(section_model, type) and issubclass(section_model, BaseModel), (
        f"{section_name}: ожидалась вложенная модель"
    )
    return section_model.model_fields[field_name]


def _kind_and_choices(info: FieldInfo) -> tuple[FieldKind, list[str]]:
    annotation = info.annotation
    if get_origin(annotation) is Literal:
        return "enum", [str(value) for value in get_args(annotation)]
    if isinstance(annotation, type) and issubclass(annotation, str):
        # StrEnum (AutonomyMode) — тоже строка, но с фиксированным набором.
        members = getattr(annotation, "__members__", None)
        if members:
            return "enum", [str(member.value) for member in members.values()]
        return "str", []
    if annotation is bool:
        return "bool", []
    if annotation is int:
        return "int", []
    if annotation is float:
        return "float", []
    return "str", []


def _bounds(info: FieldInfo) -> tuple[float | None, float | None]:
    minimum: float | None = None
    maximum: float | None = None
    for meta in info.metadata:
        for attr, target in (("gt", "min"), ("ge", "min"), ("lt", "max"), ("le", "max")):
            bound = getattr(meta, attr, None)
            if bound is None:
                continue
            if target == "min":
                minimum = float(bound)
            else:
                maximum = float(bound)
    return minimum, maximum


def _current(cfg: SvarogConfig, path: str) -> Any:
    section_name, _, field_name = path.partition(".")
    value = getattr(getattr(cfg, section_name), field_name)
    # StrEnum сериализуется как строка — форма работает с примитивами.
    return value.value if hasattr(value, "value") else value


def describe_config(cfg: SvarogConfig, config_path: str) -> ConfigView:
    """Текущее состояние формы: значения из конфига, ограничения из схемы."""
    sections: list[ConfigSectionView] = []
    for key, title, entries in _LAYOUT:
        fields: list[ConfigFieldView] = []
        for path, label, help_text in entries:
            info = _field_info(path)
            kind, choices = _kind_and_choices(info)
            minimum, maximum = _bounds(info)
            fields.append(
                ConfigFieldView(
                    path=path,
                    label=label,
                    help=help_text,
                    kind=kind,
                    value=_current(cfg, path),
                    choices=choices,
                    minimum=minimum,
                    maximum=maximum,
                )
            )
        sections.append(ConfigSectionView(key=key, title=title, fields=fields))
    return ConfigView(path=config_path, sections=sections)


def editable_paths() -> set[str]:
    return {path for _, _, entries in _LAYOUT for path, _, _ in entries}


def apply_values(raw: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Наложить изменения формы на mapping из yaml-файла.

    Возвращает новый mapping; исходный не меняется. Неизвестный путь —
    ошибка, а не молчаливое игнорирование: иначе опечатка в клиенте тихо
    ничего не сделает.
    """
    allowed = editable_paths()
    updated = {key: dict(value) if isinstance(value, dict) else value for key, value in raw.items()}
    for path, value in values.items():
        if path not in allowed:
            raise ValueError(f"поле '{path}' недоступно для правки через интерфейс")
        section_name, _, field_name = path.partition(".")
        section = updated.get(section_name)
        section = dict(section) if isinstance(section, dict) else {}
        section[field_name] = value
        updated[section_name] = section
    return updated


def _scalar(value: Any) -> str:
    """Значение в yaml-виде: true/false, число, строка без лишних кавычек."""
    dumped = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True).strip()
    # safe_dump скалярам добавляет хвост "\n...\n" — он уже снят strip'ом,
    # но списковый маркер документа может остаться.
    return dumped.removesuffix("...").strip()


_SECTION_RE = re.compile(r"^(?P<name>[A-Za-z_][\w-]*):\s*(?P<rest>.*)$")


def patch_yaml_text(text: str, values: dict[str, Any]) -> str:
    """Заменить значения ключей, не трогая остальной файл.

    Пересборка через `yaml.safe_dump` переписала бы файл целиком: пропали бы
    комментарии, пустые строки и порядок — а `svarog.yaml` человек ведёт
    руками и держит в Git. Поэтому правится ровно та строка, где лежит ключ;
    хвостовой комментарий сохраняется.

    Работает для путей `секция.поле` — только такие и редактируются формой.
    """
    lines = text.splitlines()
    # Границы секций верхнего уровня: имя → (строка заголовка, конец блока).
    bounds: dict[str, tuple[int, int]] = {}
    current: str | None = None
    start = 0
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match is None:
            continue
        if current is not None:
            bounds[current] = (start, index)
        current = match.group("name")
        start = index
    if current is not None:
        bounds[current] = (start, len(lines))

    for path, value in values.items():
        section_name, _, field_name = path.partition(".")
        rendered = _scalar(value)
        span = bounds.get(section_name)

        if span is None:
            # Секции нет — дописываем в конец, не трогая существующее.
            lines.extend(["", f"{section_name}:", f"  {field_name}: {rendered}"])
            # Заголовок — предпоследняя строка, а не пустая перед ним: иначе
            # следующий ключ этой же секции уедет в конец предыдущей.
            bounds[section_name] = (len(lines) - 2, len(lines))
            continue

        head, end = span
        field_re = re.compile(rf"^(?P<indent>\s+){re.escape(field_name)}:\s*(?P<value>.*)$")
        for index in range(head + 1, end):
            found = field_re.match(lines[index])
            if found is None:
                continue
            # Хвостовой комментарий — часть строки, которую человек написал.
            comment = ""
            tail = found.group("value")
            hash_at = tail.find("#")
            if hash_at != -1:
                comment = "   " + tail[hash_at:].rstrip()
            lines[index] = f"{found.group('indent')}{field_name}: {rendered}{comment}"
            break
        else:
            # Ключа в секции нет — вставляем первым, с отступом соседей.
            indent = "  "
            for index in range(head + 1, end):
                probe = re.match(r"^(\s+)\S", lines[index])
                if probe is not None:
                    indent = probe.group(1)
                    break
            lines.insert(head + 1, f"{indent}{field_name}: {rendered}")
            bounds = {
                name: (s if s <= head else s + 1, e if e <= head else e + 1)
                for name, (s, e) in bounds.items()
            }

    return "\n".join(lines) + "\n"


def diff_lines(before: str, after: str) -> list[DiffLine]:
    """Построчный дифф двух версий файла для панели «будет записано»."""
    lines: list[DiffLine] = []
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            lines.extend(DiffLine(kind="del", text=line) for line in before.splitlines()[i1:i2])
        if tag in ("replace", "insert"):
            lines.extend(DiffLine(kind="add", text=line) for line in after.splitlines()[j1:j2])
        if tag == "equal":
            lines.extend(DiffLine(kind="same", text=line) for line in before.splitlines()[i1:i2])
    return lines
