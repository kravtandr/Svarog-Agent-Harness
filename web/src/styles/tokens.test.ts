import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// От корня web/: в jsdom-окружении import.meta.url не файловый URL.
const css = readFileSync(join(process.cwd(), "src/styles/tokens.css"), "utf8");

describe("токены", () => {
  it("совпадают со спеком", () => {
    const expected: Record<string, string> = {
      "--bg": "#1a1917",
      "--surface": "#211f1d",
      "--raised": "#292724",
      "--line": "#322f2b",
      "--line-soft": "#262421",
      "--text": "#eae5dc",
      "--muted": "#a29b90",
      "--faint": "#6e6862",
      "--ember": "#d2622c",
      "--ok": "#6e9b72",
      "--bad": "#c4635c",
      "--git": "#7e93b8",
    };
    for (const [name, value] of Object.entries(expected)) {
      expect(css).toContain(`${name}: ${value};`);
    }
  });

  it("не содержит второго акцентного оранжевого", () => {
    // Ищем по тону, а не по первой цифре: прежняя регулярка /#[dD]…/
    // пропустила бы #e2762c. Оранжевый — это hue примерно 15–45°.
    const hexes = css.match(/#[0-9a-fA-F]{6}/g) ?? [];
    const oranges = hexes.filter((hex) => {
      const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
      const max = Math.max(r, g, b);
      const min = Math.min(r, g, b);
      if (max === min) return false;
      const chroma = (max - min) / max;
      if (chroma < 0.35) return false; // блёклый — не акцент
      let hue = 0;
      if (max === r) hue = (60 * (g - b)) / (max - min);
      else if (max === g) hue = 60 * (2 + (b - r) / (max - min));
      else hue = 60 * (4 + (r - g) / (max - min));
      if (hue < 0) hue += 360;
      return hue >= 15 && hue <= 45;
    });
    expect(oranges).toEqual(["#d2622c"]);
  });
});
