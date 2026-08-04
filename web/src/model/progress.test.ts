import { describe, expect, it } from "vitest";

import { formatElapsed, progressLabel } from "./progress";

describe("formatElapsed", () => {
  it("формат м:сс", () => {
    expect(formatElapsed(0)).toBe("0:00");
    expect(formatElapsed(7)).toBe("0:07");
    expect(formatElapsed(83)).toBe("1:23");
    expect(formatElapsed(3671)).toBe("61:11");
  });
});

describe("progressLabel", () => {
  it("без прогресса — только секундомер", () => {
    expect(progressLabel(7, null)).toBe("Сварог работает… 0:07");
  });

  it("токены с разделителем тысяч", () => {
    expect(progressLabel(83, { tokens: 12400, costUsd: 0 })).toBe(
      "Сварог работает… 1:23 · 12 400 токенов",
    );
  });

  it("стоимость видна, когда доросла до цента", () => {
    expect(progressLabel(83, { tokens: 12400, costUsd: 0.04 })).toBe(
      "Сварог работает… 1:23 · 12 400 токенов · $0.04",
    );
  });

  it("нулевые токены не показываются (bridge ещё пуст)", () => {
    expect(progressLabel(5, { tokens: 0, costUsd: 0 })).toBe(
      "Сварог работает… 0:05",
    );
  });
});
