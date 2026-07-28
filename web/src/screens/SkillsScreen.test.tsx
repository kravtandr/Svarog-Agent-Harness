import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { fakeApi } from "../test/fakeApi";
import { SkillsScreen } from "./SkillsScreen";

const skills = [
  {
    name: "git-flow",
    description: "Ветка задачи, коммит и push по политике.",
    version: "1.2.0",
    risk: "medium",
  },
  {
    name: "python-testing",
    description: "Прогон pytest и разбор падений.",
    version: "0.9.0",
    risk: "low",
  },
];

describe("экран скиллов", () => {
  it("показывает карточки с версией и риском", async () => {
    render(
      <SkillsScreen api={fakeApi({ skills: () => Promise.resolve(skills) })} />,
    );

    expect(await screen.findByText("git-flow")).toBeInTheDocument();
    expect(screen.getByText("1.2.0")).toBeInTheDocument();
    expect(screen.getByText("средний риск")).toBeInTheDocument();
    expect(screen.getByText("низкий риск")).toBeInTheDocument();
    expect(screen.getByText("2 скилла")).toBeInTheDocument();
  });

  it("на пустом списке приглашает положить скиллы, а не сообщает «нет данных»", async () => {
    render(<SkillsScreen api={fakeApi()} />);

    expect(
      await screen.findByText(/положите их в каталог skills/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/нет данных/i)).not.toBeInTheDocument();
  });

  it("сообщает об ошибке загрузки", async () => {
    const api = fakeApi({
      skills: vi.fn().mockRejectedValue(new Error("нет связи")),
    });
    render(<SkillsScreen api={api} />);

    expect(
      await screen.findByText(/не удалось загрузить скиллы/i),
    ).toBeInTheDocument();
  });
});
