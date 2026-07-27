import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { ThreadItem } from "../model/thread";
import { ToolCalls } from "./ToolCalls";

type Call = Extract<ThreadItem, { kind: "call" }>;

const call = (over: Partial<Call> = {}): Call => ({
  kind: "call",
  id: "c1",
  server: null,
  name: "write_file",
  arg: "memory/index.py",
  result: "записано 1234 символов",
  status: "ok",
  ...over,
});

describe("вызовы инструментов", () => {
  it("показывает имя, аргумент и результат", () => {
    render(<ToolCalls calls={[call()]} />);
    expect(screen.getByText("write_file")).toBeInTheDocument();
    expect(screen.getByText("memory/index.py")).toBeInTheDocument();
    expect(screen.getByText("записано 1234 символов")).toBeInTheDocument();
  });

  it("ставит имя MCP-сервера перед названием и не рисует значок", () => {
    render(
      <ToolCalls
        calls={[
          call({ server: "github", name: "list_issues", result: "2 задачи" }),
        ]}
      />,
    );
    expect(screen.getByText("github")).toBeInTheDocument();
    expect(screen.getByText("list_issues")).toBeInTheDocument();
    expect(screen.queryByText(/MCP/i)).not.toBeInTheDocument();
  });

  it("не пишет «успешно» вместо результата", () => {
    render(<ToolCalls calls={[call()]} />);
    expect(screen.queryByText(/успешно/i)).not.toBeInTheDocument();
  });

  it("раскрывает упавший вызов сразу, а успешный — по нажатию", async () => {
    const { rerender } = render(<ToolCalls calls={[call()]} />);
    expect(screen.queryByTestId("call-detail")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /write_file/ }));
    expect(screen.getByTestId("call-detail")).toBeInTheDocument();

    rerender(
      <ToolCalls calls={[call({ status: "error", result: "exit code 1" })]} />,
    );
    expect(screen.getByTestId("call-detail")).toBeInTheDocument();
  });

  it("ничего не рисует на пустом списке", () => {
    const { container } = render(<ToolCalls calls={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
