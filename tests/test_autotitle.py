"""Юнит-тесты автоназвания чатов: чистка ответа модели и fallback (спека 2026-08-04)."""

from collections.abc import Callable

from svarog_harness.gateway.autotitle import (
    clean_title,
    fallback_title,
    generate_title,
    needs_draft,
    needs_refine,
    title_for,
)
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolDefinition,
    Usage,
)


class OneShotProvider(ModelProvider):
    def __init__(self, content: str = "", *, error: bool = False) -> None:
        self.content = content
        self.error = error
        self.calls: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.calls.append(list(messages))
        if self.error:
            raise RuntimeError("aux недоступна")
        return CompletionResult(content=self.content, usage=Usage(1, 1))


def test_clean_title_strips_quotes_period_and_newlines() -> None:
    assert clean_title("«Настройка  CI»\n") == "Настройка CI"
    assert clean_title('"Deploy pipeline."') == "Deploy pipeline"
    assert clean_title("Один\nдва   три") == "Один два три"


def test_clean_title_strips_unicode_curly_quotes() -> None:
    # U+201C and U+201D (left and right curly double quotes)
    curly_dbl = chr(0x201C) + "Умный заголовок" + chr(0x201D)
    assert clean_title(curly_dbl) == "Умный заголовок"
    # U+2018 and U+2019 (left and right curly single quotes)
    curly_sgl = chr(0x2018) + "Ещё один заголовок" + chr(0x2019)
    assert clean_title(curly_sgl) == "Ещё один заголовок"
    # Mixed curly and straight quotes
    mixed = chr(0x201C) + "Mixed «quotes» here" + chr(0x201D)
    assert clean_title(mixed) == "Mixed «quotes» here"


def test_clean_title_garbage_is_none() -> None:
    assert clean_title("  \n") is None
    assert clean_title("«».") is None


def test_clean_title_cuts_to_200() -> None:
    cut = clean_title("х" * 500)
    assert cut is not None and len(cut) == 200


def test_fallback_title_cuts_on_word_boundary() -> None:
    text = "напиши длинное сочинение про кота который жил на крыше дома и ловил голубей"
    result = fallback_title(text)
    assert result is not None
    assert result.endswith("…") and len(result) <= 61
    assert not result[:-1].endswith(" ")


def test_fallback_title_short_text_kept_as_is() -> None:
    assert fallback_title("почини баг") == "почини баг"
    assert fallback_title("   ") is None


def test_needs_draft_only_for_default_titles_without_flag() -> None:
    assert needs_draft("Новый чат", None)
    assert needs_draft("gateway-сессия", {})
    assert needs_draft("", {})
    assert needs_draft(None, {})
    assert not needs_draft("Мой чат", {})
    assert not needs_draft("Новый чат", {"autotitle": "draft"})
    assert not needs_draft("Новый чат", {"autotitle": "done"})


def test_needs_refine_after_draft() -> None:
    meta = {"autotitle": "draft", "autotitle_draft": "Черновик"}
    assert needs_refine("Черновик", meta)
    # Черновик переименовали вручную (CLI) — не перетираем.
    assert not needs_refine("Моё имя", meta)


def test_needs_refine_without_draft_uses_default_condition() -> None:
    assert needs_refine("Новый чат", {})
    assert needs_refine("gateway-сессия", None)
    assert not needs_refine("Мой чат", {})


def test_needs_refine_final_flags_block() -> None:
    assert not needs_refine("Название", {"autotitle": "done"})
    assert not needs_refine("Название", {"autotitle": "fallback"})


async def test_generate_title_happy_path() -> None:
    provider = OneShotProvider("«География Франции.»")
    title = await generate_title(provider, "Какая столица Франции?", "Париж")
    assert title == "География Франции"
    body = provider.calls[0][1].content
    assert "Какая столица Франции?" in body
    assert "Париж" in body


async def test_generate_title_error_returns_none() -> None:
    provider = OneShotProvider(error=True)
    assert await generate_title(provider, "вопрос", "ответ") is None


async def test_generate_title_without_answer_omits_answer_block() -> None:
    provider = OneShotProvider("Название")
    assert await generate_title(provider, "вопрос", "") == "Название"
    assert "Ответ:" not in provider.calls[0][1].content


async def test_generate_title_truncates_long_input() -> None:
    provider = OneShotProvider("Название")
    await generate_title(provider, "в" * 5000, "о" * 5000)
    body = provider.calls[0][1].content
    assert len(body) < 3200  # 2000 (вопрос) + 1000 (ответ) + разметка


async def test_title_for_factory_error_returns_none() -> None:
    def broken_factory() -> ModelProvider:
        raise RuntimeError("нет aux-провайдера в конфиге")

    assert await title_for(broken_factory, "вопрос", "ответ") is None


async def test_title_for_happy_path() -> None:
    provider = OneShotProvider("Название")
    assert await title_for(lambda: provider, "вопрос", "ответ") == "Название"
