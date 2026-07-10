"""Тесты update_plan: run-local план для сложных задач."""

from svarog_harness.tools.plan_tools import UpdatePlanTool


async def test_update_plan_accepts_single_in_progress() -> None:
    updates: list[dict[str, object]] = []
    tool = UpdatePlanTool(lambda items, note: updates.append({"items": items, "note": note}))

    result = await tool.call(
        {
            "items": [
                {"id": "inspect", "text": "изучить код", "status": "completed"},
                {"id": "tests", "text": "запустить тесты", "status": "in_progress"},
            ],
            "note": "двигаюсь к проверке",
        }
    )

    assert result.ok
    assert updates == [
        {
            "items": [
                {"id": "inspect", "text": "изучить код", "status": "completed"},
                {"id": "tests", "text": "запустить тесты", "status": "in_progress"},
            ],
            "note": "двигаюсь к проверке",
        }
    ]
    assert updates[0]["note"] == "двигаюсь к проверке"


async def test_update_plan_rejects_multiple_in_progress() -> None:
    tool = UpdatePlanTool(lambda items, note: None)

    result = await tool.call(
        {
            "items": [
                {"id": "a", "text": "первый", "status": "in_progress"},
                {"id": "b", "text": "второй", "status": "in_progress"},
            ]
        }
    )

    assert not result.ok
    assert result.error is not None
    assert "только один пункт" in result.error
