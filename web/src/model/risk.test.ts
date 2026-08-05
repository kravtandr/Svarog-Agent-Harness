import { describe, expect, it } from "vitest";

import {
  MCP_RISK_CONSEQUENCE,
  RISK_LEVELS,
  riskClass,
  riskLabel,
} from "./risk";

describe("шкала риска", () => {
  it("уровни идут от низкого к критичному", () => {
    expect(RISK_LEVELS).toEqual(["low", "medium", "high", "critical"]);
  });

  it("подписывает известный уровень по-русски", () => {
    expect(riskLabel("high")).toBe("высокий риск");
  });

  it("неизвестный уровень показывает как есть, а не прячет", () => {
    expect(riskLabel("странный")).toBe("странный");
    expect(riskClass("странный")).toBe("risk--unknown");
  });

  it("даёт класс на каждый уровень", () => {
    expect(riskClass("critical")).toBe("risk--critical");
  });

  it("объясняет последствие для каждого уровня MCP", () => {
    for (const level of RISK_LEVELS) {
      expect(MCP_RISK_CONSEQUENCE[level].length).toBeGreaterThan(20);
    }
  });
});
