import { describe, expect, it } from "vitest";

import { parsePaste } from "./mcpPaste";
import { MCP_PRESETS } from "./mcpPresets";
import { RISK_LEVELS } from "./risk";

describe("каталог MCP-пресетов", () => {
  it("не пуст и без повторов id", () => {
    expect(MCP_PRESETS.length).toBeGreaterThanOrEqual(8);
    const ids = MCP_PRESETS.map((preset) => preset.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("каждая строка вставки разбирается парсером", () => {
    for (const preset of MCP_PRESETS) {
      const parsed = parsePaste(preset.paste);
      expect(parsed, preset.id).not.toBeNull();
      expect(parsed?.command, preset.id).not.toBe("");
    }
  });

  it("риск каждого пресета — из известной шкалы", () => {
    for (const preset of MCP_PRESETS) {
      expect(RISK_LEVELS).toContain(preset.risk);
    }
  });

  it("в строке вставки нет значений секретов", () => {
    for (const preset of MCP_PRESETS) {
      expect(preset.paste, preset.id).not.toMatch(/[A-Z_]{4,}=\S/);
    }
  });
});
