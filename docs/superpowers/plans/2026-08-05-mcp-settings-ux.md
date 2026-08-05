# UX вкладок MCP и Настроек Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Переделать вкладки MCP и Настроек по спеке [2026-08-05-mcp-settings-ux-design.md](../specs/2026-08-05-mcp-settings-ux-design.md): вставка одной строкой вместо пяти полей, каталог пресетов, карточки серверов сеткой, дифф-полоса вместо постоянной колонки.

**Architecture:** Только фронтенд. Логика без DOM (парсер вставки, пресеты, шкала риска) выносится в `web/src/model/*` и тестируется отдельно от экранов. Экраны переписываются поверх этих модулей. Бэкенд, схема `svarog.yaml` и контракты API не меняются: живость сохранённого сервера получается вызовом существующего `POST /mcp/test` с полями сервера из `GET /mcp`.

**Tech Stack:** React 19, TypeScript 5.7, Vite 6, Vitest 3, @testing-library/react, CSS без препроцессора (токены в `web/src/styles/tokens.css`).

## Global Constraints

- Все команды выполняются из каталога `web/`.
- Тест одного файла: `npx vitest run src/путь/файл.test.tsx`. Полная проверка: `npm test` — она включает `tsc --noEmit`, `prettier --check src` и `vitest run`.
- **Перед каждым коммитом выполнять `npm run format`** — иначе `npm test` упадёт на `prettier --check`.
- Язык интерфейса и комментариев — русский. Комментарии объясняют «почему», а не «что» (стиль существующего кода).
- Цвета только через переменные из `web/src/styles/tokens.css`. Новых цветовых переменных не заводить, кроме одной, указанной в задаче 1.
- Акцент `--ember` — только активная сессия, гейт, отправка и контур лого (`tokens.css:12`). Ни риск, ни бейджи его не используют.
- Значения секретов никогда не попадают ни в форму, ни в `mcpAdd`: из вставленного JSON берутся только имена ключей `env`.
- Работа ведётся в ветке `mcp-settings-ux` (уже создана, в ней лежит спека).

---

### Task 1: Общая шкала риска

Выделяет уровни, подписи и цвета риска в один модуль и переводит на него `SkillsScreen`. Дальше им пользуются задачи 4 и 5.

**Files:**
- Create: `web/src/model/risk.ts`
- Create: `web/src/model/risk.test.ts`
- Modify: `web/src/styles/base.css` (добавить классы `.risk--*` в конец файла)
- Modify: `web/src/screens/SkillsScreen.tsx:8-12,51-53`
- Modify: `web/src/screens/SkillsScreen.css:39-50`
- Modify: `web/src/screens/SkillsScreen.test.tsx`

**Interfaces:**
- Consumes: ничего.
- Produces: `RISK_LEVELS: readonly RiskLevel[]`, `type RiskLevel = "low" | "medium" | "high" | "critical"`, `MCP_RISK_CONSEQUENCE: Record<RiskLevel, string>`, `riskLabel(level: string): string`, `riskClass(level: string): string`.

- [ ] **Step 1: Написать падающий тест**

Создать `web/src/model/risk.test.ts`:

```ts
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
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `npx vitest run src/model/risk.test.ts`
Expected: FAIL — `Failed to resolve import "./risk"`.

- [ ] **Step 3: Написать модуль**

Создать `web/src/model/risk.ts`:

```ts
export const RISK_LEVELS = ["low", "medium", "high", "critical"] as const;

export type RiskLevel = (typeof RISK_LEVELS)[number];

const LABELS: Record<RiskLevel, string> = {
  low: "низкий риск",
  medium: "средний риск",
  high: "высокий риск",
  critical: "критичный риск",
};

/**
 * Что уровень риска реально меняет для MCP-инструмента.
 *
 * Сверено с policy/engine.py: ветка `action_type.startswith("mcp.")`
 * (engine.py:207-214, §9) требует approval для любого MCP-вызова раньше,
 * чем риск успевает что-либо решить. Поэтому уровень отвечает не на
 * вопрос «спросят ли», а на вопрос «можно ли это ослабить».
 */
export const MCP_RISK_CONSEQUENCE: Record<RiskLevel, string> = {
  low: "Подтверждение по умолчанию; правилом notify в svarog.yaml ослабляется до уведомления.",
  medium:
    "Подтверждение по умолчанию; правилом notify в svarog.yaml ослабляется до уведомления.",
  high: "Подтверждение по умолчанию; в режиме supervised ослабить нельзя.",
  critical:
    "Подтверждение всегда — не отключается ни правилом, ни профилем, ни режимом автономии.",
};

function known(level: string): level is RiskLevel {
  return (RISK_LEVELS as readonly string[]).includes(level);
}

/** Незнакомый уровень показываем как есть: молча подставить «низкий» значило
    бы соврать о том, чего мы не знаем. */
export function riskLabel(level: string): string {
  return known(level) ? LABELS[level] : level;
}

export function riskClass(level: string): string {
  return known(level) ? `risk--${level}` : "risk--unknown";
}
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `npx vitest run src/model/risk.test.ts`
Expected: PASS, 5 тестов.

- [ ] **Step 5: Добавить классы цвета**

В конец `web/src/styles/base.css`:

```css
/* Риск — семантика, а не акцент: цвет говорит, чем действие может обойтись.
   Одна шкала на скиллы и MCP, поэтому классы живут здесь, а не в экране. */
.risk--low {
  color: var(--ok);
}
.risk--medium {
  color: #d89a79;
}
.risk--high {
  color: var(--bad);
}
.risk--critical {
  color: #e08b84;
  font-weight: 600;
}
.risk--unknown {
  color: var(--faint);
}
```

- [ ] **Step 6: Перевести SkillsScreen на общий модуль**

В `web/src/screens/SkillsScreen.tsx` удалить локальный `RISK_LABELS` (строки 8-12) и добавить импорт:

```ts
import { riskClass, riskLabel } from "../model/risk";
```

Заменить строки 51-53 на:

```tsx
<span className={`skill__risk ${riskClass(skill.risk)}`}>
  {riskLabel(skill.risk)}
</span>
```

В `web/src/screens/SkillsScreen.css` удалить правила `.skill__risk--medium` и `.skill__risk--high` (строки 45-50) и цвет из `.skill__risk`, оставив только раскладку:

```css
.skill__risk {
  margin-left: auto;
  font-size: 12px;
}
```

- [ ] **Step 7: Проверить, что тесты скиллов не сломались**

Run: `npx vitest run src/screens/SkillsScreen.test.tsx`
Expected: PASS. Тест `показывает карточки с версией и риском` ищет текст подписи; подписи low/medium/high не изменились, поэтому правок в тесте не требуется. Если тест падает — значит он ищет удалённый класс; тогда заменить проверку класса на проверку текста `riskLabel`.

- [ ] **Step 8: Коммит**

```bash
npm run format
npm test
git add src/model/risk.ts src/model/risk.test.ts src/styles/base.css src/screens/SkillsScreen.tsx src/screens/SkillsScreen.css
git commit -m "feat(web): общая шкала риска для скиллов и MCP"
```

---

### Task 2: Парсер вставки MCP-конфига

Чистая функция, превращающая вставленный текст в поля сервера. Ядро вкладки — тестируется без React.

**Files:**
- Create: `web/src/model/mcpPaste.ts`
- Create: `web/src/model/mcpPaste.test.ts`

**Interfaces:**
- Consumes: ничего.
- Produces: `interface ParsedServer { name: string; command: string; args: string[]; envRefs: string[] }` и `parsePaste(text: string): ParsedServer | null`.

- [ ] **Step 1: Написать падающие тесты**

Создать `web/src/model/mcpPaste.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { parsePaste } from "./mcpPaste";

describe("разбор вставленного MCP-конфига", () => {
  it("режет shell-строку на команду и аргументы", () => {
    expect(parsePaste("uvx mcp-server-fetch")).toEqual({
      name: "fetch",
      command: "uvx",
      args: ["mcp-server-fetch"],
      envRefs: [],
    });
  });

  it("уважает кавычки в аргументах", () => {
    expect(parsePaste('npx server --root "/Мои файлы/проект"')?.args).toEqual([
      "server",
      "--root",
      "/Мои файлы/проект",
    ]);
  });

  it("выводит имя из scoped-пакета", () => {
    expect(parsePaste("npx -y @modelcontextprotocol/server-github")?.name).toBe(
      "github",
    );
  });

  it("читает блок mcpServers из конфига Claude Desktop", () => {
    const text = JSON.stringify({
      mcpServers: {
        github: {
          command: "npx",
          args: ["-y", "@modelcontextprotocol/server-github"],
          env: { GITHUB_TOKEN: "ghp_живой_токен" },
        },
      },
    });
    expect(parsePaste(text)).toEqual({
      name: "github",
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-github"],
      envRefs: ["GITHUB_TOKEN"],
    });
  });

  it("значение секрета из вставки не сохраняется нигде", () => {
    const text = JSON.stringify({
      mcpServers: { gh: { command: "npx", env: { TOKEN: "ghp_секрет" } } },
    });
    expect(JSON.stringify(parsePaste(text))).not.toContain("ghp_секрет");
  });

  it("читает одиночный объект сервера", () => {
    const text = '{"command": "uvx", "args": ["mcp-server-time"]}';
    expect(parsePaste(text)).toEqual({
      name: "time",
      command: "uvx",
      args: ["mcp-server-time"],
      envRefs: [],
    });
  });

  it("на пустом и мусорном вводе возвращает null, а не полупустой объект", () => {
    expect(parsePaste("   ")).toBeNull();
    expect(parsePaste("{ поломанный json")).toBeNull();
    expect(parsePaste('{"args": ["без-команды"]}')).toBeNull();
  });

  it("если имя вывести не из чего — берёт команду", () => {
    expect(parsePaste("my-server")?.name).toBe("my-server");
  });
});
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `npx vitest run src/model/mcpPaste.test.ts`
Expected: FAIL — `Failed to resolve import "./mcpPaste"`.

- [ ] **Step 3: Написать парсер**

Создать `web/src/model/mcpPaste.ts`:

```ts
export interface ParsedServer {
  name: string;
  command: string;
  args: string[];
  envRefs: string[];
}

/** Токенизация shell-строки с уважением к кавычкам: пути с пробелами в
    аргументах MCP-серверов обычны, а split(/\s+/) их молча ломает. */
function tokenize(line: string): string[] {
  const out: string[] = [];
  let current = "";
  let quote: '"' | "'" | null = null;
  let started = false;
  for (const char of line) {
    if (quote !== null) {
      if (char === quote) quote = null;
      else current += char;
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      started = true;
      continue;
    }
    if (/\s/.test(char)) {
      if (started) out.push(current);
      current = "";
      started = false;
      continue;
    }
    current += char;
    started = true;
  }
  if (started) out.push(current);
  return out;
}

/** Имя сервера из аргументов: `@modelcontextprotocol/server-github` → github,
    `mcp-server-fetch` → fetch. Флаги (-y, --port) именем быть не могут. */
function guessName(command: string, args: string[]): string {
  for (const arg of args) {
    if (arg.startsWith("-")) continue;
    const tail = arg.split("/").pop() ?? arg;
    const stripped = tail.replace(/^(mcp-server-|server-|mcp-)/, "");
    if (stripped !== "") return stripped;
  }
  return command;
}

function fromObject(raw: unknown, fallbackName: string): ParsedServer | null {
  if (typeof raw !== "object" || raw === null) return null;
  const entry = raw as Record<string, unknown>;
  const command = typeof entry.command === "string" ? entry.command.trim() : "";
  if (command === "") return null;
  const args = Array.isArray(entry.args)
    ? entry.args.filter((item): item is string => typeof item === "string")
    : [];
  // Только имена ключей env: значения из чужого конфига — живые токены, и
  // запись их в svarog.yaml была бы тихой утечкой (секреты живут в store).
  const envRefs =
    typeof entry.env === "object" && entry.env !== null
      ? Object.keys(entry.env as Record<string, unknown>)
      : [];
  const name = fallbackName !== "" ? fallbackName : guessName(command, args);
  return { name, command, args, envRefs };
}

export function parsePaste(text: string): ParsedServer | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;

  if (trimmed.startsWith("{")) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return null;
    }
    const root = parsed as Record<string, unknown>;
    const servers = root.mcpServers;
    if (typeof servers === "object" && servers !== null) {
      const [first] = Object.entries(servers as Record<string, unknown>);
      if (first === undefined) return null;
      return fromObject(first[1], first[0]);
    }
    return fromObject(parsed, "");
  }

  const tokens = tokenize(trimmed);
  const [command, ...args] = tokens;
  if (command === undefined || command === "") return null;
  return { name: guessName(command, args), command, args, envRefs: [] };
}
```

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `npx vitest run src/model/mcpPaste.test.ts`
Expected: PASS, 8 тестов.

- [ ] **Step 5: Коммит**

```bash
npm run format
npm test
git add src/model/mcpPaste.ts src/model/mcpPaste.test.ts
git commit -m "feat(web): парсер вставки MCP-конфига без значений секретов"
```

---

### Task 3: Каталог пресетов

Константа с готовыми серверами — из неё строится пустое состояние вкладки.

**Files:**
- Create: `web/src/model/mcpPresets.ts`
- Create: `web/src/model/mcpPresets.test.ts`

**Interfaces:**
- Consumes: `RiskLevel` из `web/src/model/risk.ts` (задача 1), `parsePaste` из `web/src/model/mcpPaste.ts` (задача 2, только в тесте).
- Produces: `interface McpPreset { id: string; title: string; paste: string; risk: RiskLevel; hint: string }` и `MCP_PRESETS: McpPreset[]`.

- [ ] **Step 1: Написать падающий тест**

Создать `web/src/model/mcpPresets.test.ts`:

```ts
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
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `npx vitest run src/model/mcpPresets.test.ts`
Expected: FAIL — `Failed to resolve import "./mcpPresets"`.

- [ ] **Step 3: Написать каталог**

Создать `web/src/model/mcpPresets.ts`:

```ts
import type { RiskLevel } from "./risk";

export interface McpPreset {
  id: string;
  title: string;
  /** Строка ровно в том виде, в каком человек вставил бы её сам, — попадает
      в поле вставки и проходит через parsePaste, а не в обход него. */
  paste: string;
  risk: RiskLevel;
  hint: string;
}

/** Фронтовая константа, а не эндпоинт: список меняется вместе с UI,
    версионируется вместе с ним и не требует ни сети, ни миграции конфига. */
export const MCP_PRESETS: McpPreset[] = [
  {
    id: "fetch",
    title: "fetch",
    paste: "uvx mcp-server-fetch",
    risk: "medium",
    hint: "Загружает страницы по URL",
  },
  {
    id: "filesystem",
    title: "filesystem",
    paste: "npx -y @modelcontextprotocol/server-filesystem .",
    risk: "high",
    hint: "Читает и пишет файлы в указанном каталоге",
  },
  {
    id: "github",
    title: "github",
    paste: "npx -y @modelcontextprotocol/server-github",
    risk: "high",
    hint: "Issues, PR и поиск по коду; нужен GITHUB_TOKEN",
  },
  {
    id: "postgres",
    title: "postgres",
    paste: "npx -y @modelcontextprotocol/server-postgres",
    risk: "high",
    hint: "Запросы к базе; нужен DATABASE_URL",
  },
  {
    id: "playwright",
    title: "playwright",
    paste: "npx -y @playwright/mcp",
    risk: "high",
    hint: "Управляет браузером",
  },
  {
    id: "sqlite",
    title: "sqlite",
    paste: "uvx mcp-server-sqlite",
    risk: "medium",
    hint: "Запросы к локальному файлу базы",
  },
  {
    id: "memory",
    title: "memory",
    paste: "npx -y @modelcontextprotocol/server-memory",
    risk: "low",
    hint: "Граф знаний в памяти процесса",
  },
  {
    id: "time",
    title: "time",
    paste: "uvx mcp-server-time",
    risk: "low",
    hint: "Текущее время и часовые пояса",
  },
];
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `npx vitest run src/model/mcpPresets.test.ts`
Expected: PASS, 4 теста.

- [ ] **Step 5: Коммит**

```bash
npm run format
npm test
git add src/model/mcpPresets.ts src/model/mcpPresets.test.ts
git commit -m "feat(web): каталог готовых MCP-серверов"
```

---

### Task 4: Форма подключения — вставка, проверка, риск

Заменяет пять полей на поле вставки с раскрываемым уточнением, делает панель проверки главным выходом и ставит сегментированный контроль риска.

**Files:**
- Modify: `web/src/screens/McpScreen.tsx` (переписывается форма — строки 96-226; список серверов пока оставить как есть, его переделывает задача 5)
- Modify: `web/src/screens/McpScreen.css`
- Modify: `web/src/screens/McpScreen.test.tsx`

**Interfaces:**
- Consumes: `parsePaste`, `ParsedServer` (задача 2); `MCP_PRESETS` (задача 3); `MCP_RISK_CONSEQUENCE`, `RISK_LEVELS`, `RiskLevel` (задача 1); `api.mcpTest`, `api.mcpAdd` из `web/src/api/client.ts:87-100`.
- Produces: разметку с ярлыками `Команда или JSON`, `Имя`, `Команда`, `Аргументы`, `Секреты (имена через запятую)`; кнопки `Проверить`, `Подключить`, `Уточнить`; радиогруппу `Риск инструментов`.

- [ ] **Step 1: Написать падающие тесты**

Заменить содержимое `web/src/screens/McpScreen.test.tsx` тестами формы (тесты списка серверов добавит задача 5):

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fakeApi } from "../test/fakeApi";
import { McpScreen } from "./McpScreen";

describe("вкладка MCP: подключение", () => {
  it("вставленная строка разбирается на поля", async () => {
    render(<McpScreen api={fakeApi()} />);
    await userEvent.type(
      screen.getByLabelText("Команда или JSON"),
      "uvx mcp-server-fetch",
    );
    await userEvent.click(screen.getByRole("button", { name: "Уточнить" }));
    expect(screen.getByLabelText("Имя")).toHaveValue("fetch");
    expect(screen.getByLabelText("Команда")).toHaveValue("uvx");
    expect(screen.getByLabelText("Аргументы")).toHaveValue("mcp-server-fetch");
  });

  it("клик по пресету заполняет поле вставки", async () => {
    render(<McpScreen api={fakeApi()} />);
    await userEvent.click(screen.getByRole("button", { name: /github/ }));
    expect(screen.getByLabelText("Команда или JSON")).toHaveValue(
      "npx -y @modelcontextprotocol/server-github",
    );
  });

  it("успешная проверка показывает инструменты чипами", async () => {
    const api = fakeApi({
      mcpTest: vi.fn().mockResolvedValue({
        ok: true,
        tools: ["fetch", "search"],
        error: null,
      }),
    });
    render(<McpScreen api={api} />);
    await userEvent.type(
      screen.getByLabelText("Команда или JSON"),
      "uvx mcp-server-fetch",
    );
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        /Сервер ответил — 2 инструмента/,
      ),
    );
    expect(screen.getByText("search")).toBeInTheDocument();
    expect(api.mcpTest).toHaveBeenCalledWith({
      command: "uvx",
      args: ["mcp-server-fetch"],
      env_refs: [],
    });
  });

  it("провал проверки показан честно", async () => {
    const api = fakeApi({
      mcpTest: vi.fn().mockResolvedValue({
        ok: false,
        tools: [],
        error: "команда не найдена",
      }),
    });
    render(<McpScreen api={api} />);
    await userEvent.type(screen.getByLabelText("Команда или JSON"), "нет-такой");
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("команда не найдена"),
    );
  });

  it("после провала проверки подключение требует второго клика", async () => {
    const api = fakeApi({
      mcpTest: vi
        .fn()
        .mockResolvedValue({ ok: false, tools: [], error: "нет бинаря" }),
    });
    render(<McpScreen api={api} />);
    await userEvent.type(screen.getByLabelText("Команда или JSON"), "нет-такой");
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("нет бинаря"),
    );
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
    expect(api.mcpAdd).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Всё равно подключить?" }),
    );
    await waitFor(() => expect(api.mcpAdd).toHaveBeenCalled());
  });

  it("правка команды сбрасывает результат проверки", async () => {
    const api = fakeApi({
      mcpTest: vi
        .fn()
        .mockResolvedValue({ ok: true, tools: ["fetch"], error: null }),
    });
    render(<McpScreen api={api} />);
    const paste = screen.getByLabelText("Команда или JSON");
    await userEvent.type(paste, "uvx mcp-server-fetch");
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    await userEvent.type(paste, "-другое");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("значение секрета из вставленного JSON не уходит в сохранение", async () => {
    const api = fakeApi();
    render(<McpScreen api={api} />);
    await userEvent.click(screen.getByLabelText("Команда или JSON"));
    await userEvent.paste(
      JSON.stringify({
        mcpServers: {
          gh: {
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-github"],
            env: { GITHUB_TOKEN: "ghp_живой" },
          },
        },
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
    await waitFor(() =>
      expect(api.mcpAdd).toHaveBeenCalledWith({
        name: "gh",
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-github"],
        env_refs: ["GITHUB_TOKEN"],
        risk: "high",
      }),
    );
    expect(JSON.stringify(vi.mocked(api.mcpAdd).mock.calls)).not.toContain(
      "ghp_живой",
    );
  });

  it("новая проверка отменяет выданное согласие", async () => {
    const api = fakeApi({
      mcpTest: vi
        .fn()
        .mockResolvedValue({ ok: false, tools: [], error: "нет бинаря" }),
    });
    render(<McpScreen api={api} />);
    await userEvent.type(screen.getByLabelText("Команда или JSON"), "нет-такой");
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("нет бинаря"),
    );
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
    expect(
      screen.getByRole("button", { name: "Всё равно подключить?" }),
    ).toBeInTheDocument();

    // Повторная проверка — согласие сброшено, второй отказ спрашивает заново.
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Подключить" })).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
    expect(api.mcpAdd).not.toHaveBeenCalled();
  });

  it("выбранный риск объясняет последствие", async () => {
    render(<McpScreen api={fakeApi()} />);
    await userEvent.click(screen.getByRole("radio", { name: "critical" }));
    expect(screen.getByText(/не отключается ни правилом/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `npx vitest run src/screens/McpScreen.test.tsx`
Expected: FAIL — `Unable to find a label with the text of: Команда или JSON`.

- [ ] **Step 3: Переписать форму**

В `web/src/screens/McpScreen.tsx` заменить состояние и разметку формы. Состояние вместо пяти строк:

```tsx
const [paste, setPaste] = useState("");
const [detailsOpen, setDetailsOpen] = useState(false);
const [override, setOverride] = useState<Partial<ParsedServer> | null>(null);
const [risk, setRisk] = useState<RiskLevel>("high");
const [test, setTest] = useState<McpTest | null>(null);
const [testing, setTesting] = useState(false);
const [forcing, setForcing] = useState(false);
const [status, setStatus] = useState<string | null>(null);
```

Разобранный сервер — производная от вставки и ручных правок:

```tsx
// Ручная правка перекрывает разбор, но не отменяет его: человек мог
// поправить одно поле из четырёх, и остальные должны остаться живыми.
const parsed = parsePaste(paste);
const draft: ParsedServer = {
  name: override?.name ?? parsed?.name ?? "",
  command: override?.command ?? parsed?.command ?? "",
  args: override?.args ?? parsed?.args ?? [],
  envRefs: override?.envRefs ?? parsed?.envRefs ?? [],
};
```

Правка вставки сбрасывает и ручные перекрытия, и результат проверки:

```tsx
const editPaste = (value: string) => {
  setPaste(value);
  setOverride(null);
  setTest(null);
  setForcing(false);
};

const editField = (part: Partial<ParsedServer>) => {
  setOverride({ ...(override ?? {}), ...part });
  setTest(null);
  setForcing(false);
};
```

Проверка и подключение:

```tsx
const runTest = async () => {
  setTesting(true);
  setTest(null);
  // Согласие «всё равно подключить» выдано под конкретный провал: новая
  // проверка его отменяет, иначе второй отказ прошёл бы без переспроса.
  setForcing(false);
  try {
    setTest(
      await api.mcpTest({
        command: draft.command,
        args: draft.args,
        env_refs: draft.envRefs,
      }),
    );
  } catch (exc: unknown) {
    setTest({
      ok: false,
      tools: [],
      error: exc instanceof ApiError ? exc.message : "проверка не удалась",
    });
  } finally {
    setTesting(false);
  }
};

const save = async () => {
  // Проверка не обязательна, но провалившаяся — повод переспросить: молча
  // записать заведомо нерабочий сервер значит спрятать ошибку до запуска.
  if (test !== null && !test.ok && !forcing) {
    setForcing(true);
    return;
  }
  setStatus(null);
  try {
    await api.mcpAdd({
      name: draft.name,
      command: draft.command,
      args: draft.args,
      env_refs: draft.envRefs,
      risk,
    });
    setStatus(`Сервер «${draft.name}» сохранён в svarog.yaml.`);
    setPaste("");
    setOverride(null);
    setTest(null);
    setForcing(false);
    reload();
  } catch (exc: unknown) {
    setStatus(
      exc instanceof ApiError ? exc.message : "Не удалось сохранить сервер.",
    );
  }
};
```

Разметка формы (заменяет строки 127-221 старого файла):

```tsx
<h3 className="settings__title">Подключить сервер</h3>
<div className="field">
  <label className="field__label" htmlFor="mcp-paste">
    Команда или JSON
  </label>
  <input
    id="mcp-paste"
    className="field__control"
    value={paste}
    placeholder="uvx mcp-server-fetch"
    onChange={(e) => editPaste(e.target.value)}
  />
</div>

{paste.trim() === "" && (
  <div className="mcp__presets">
    {MCP_PRESETS.map((preset) => (
      <button
        key={preset.id}
        type="button"
        className="mcp__preset"
        onClick={() => {
          editPaste(preset.paste);
          setRisk(preset.risk);
        }}
      >
        <span className="mcp__preset-title">{preset.title}</span>
        <span className="mcp__preset-hint">{preset.hint}</span>
      </button>
    ))}
  </div>
)}

<button
  type="button"
  className="btn btn--small mcp__details-toggle"
  aria-expanded={detailsOpen}
  onClick={() => setDetailsOpen(!detailsOpen)}
>
  Уточнить
</button>

{detailsOpen && (
  <div className="mcp__details">
    <div className="field">
      <label className="field__label" htmlFor="mcp-name">
        Имя
      </label>
      <input
        id="mcp-name"
        className="field__control"
        value={draft.name}
        onChange={(e) => editField({ name: e.target.value })}
      />
    </div>
    <div className="field">
      <label className="field__label" htmlFor="mcp-command">
        Команда
      </label>
      <input
        id="mcp-command"
        className="field__control"
        value={draft.command}
        onChange={(e) => editField({ command: e.target.value })}
      />
    </div>
    <div className="field">
      <label className="field__label" htmlFor="mcp-args">
        Аргументы
      </label>
      <input
        id="mcp-args"
        className="field__control"
        value={draft.args.join(" ")}
        onChange={(e) =>
          editField({ args: e.target.value.split(/\s+/).filter(Boolean) })
        }
      />
    </div>
    <div className="field">
      <label className="field__label" htmlFor="mcp-env">
        Секреты (имена через запятую)
      </label>
      <p className="field__help">
        Только имена. Значения задаются командой svarog secrets set и в
        svarog.yaml не попадают.
      </p>
      <input
        id="mcp-env"
        className="field__control"
        value={draft.envRefs.join(", ")}
        onChange={(e) =>
          editField({
            envRefs: e.target.value
              .split(",")
              .map((item) => item.trim())
              .filter(Boolean),
          })
        }
      />
    </div>
  </div>
)}

<fieldset className="mcp__risk-set">
  <legend className="field__label">Риск инструментов</legend>
  <div className="mcp__segments">
    {RISK_LEVELS.map((level) => (
      <label key={level} className="mcp__segment">
        <input
          type="radio"
          name="mcp-risk"
          value={level}
          checked={risk === level}
          onChange={() => setRisk(level)}
        />
        <span>{level}</span>
      </label>
    ))}
  </div>
  <p className="field__help">{MCP_RISK_CONSEQUENCE[risk]}</p>
</fieldset>

<div className="mcp__actions">
  <button
    type="button"
    className="btn"
    disabled={draft.command === "" || testing}
    onClick={() => void runTest()}
  >
    {testing ? "Проверяем…" : "Проверить"}
  </button>
  <button
    type="button"
    className="btn btn--primary"
    disabled={draft.name === "" || draft.command === ""}
    onClick={() => void save()}
  >
    {forcing ? "Всё равно подключить?" : "Подключить"}
  </button>
</div>

{test !== null && (
  <div className="mcp__result" role="status">
    {test.ok ? (
      <>
        <p className="mcp__result-head">
          Сервер ответил — {counted(test.tools.length, "инструмент", "инструмента", "инструментов")}
        </p>
        <div className="mcp__chips">
          {test.tools.map((tool) => (
            <span key={tool} className="mcp__chip">
              {tool}
            </span>
          ))}
        </div>
      </>
    ) : (
      <p className="field__error">Не подключился: {test.error}</p>
    )}
  </div>
)}
{status !== null && <p className="field__help">{status}</p>}
```

Импорты в шапке файла:

```tsx
import { counted } from "../model/plural";
import { parsePaste, type ParsedServer } from "../model/mcpPaste";
import { MCP_PRESETS } from "../model/mcpPresets";
import { MCP_RISK_CONSEQUENCE, RISK_LEVELS, type RiskLevel } from "../model/risk";
```

Константу `RISKS` (строка 8) и функции `parsedArgs`/`parsedEnv` (строки 32-37) удалить. Вводный абзац (строки 100-103) заменить на:

```tsx
<p className="field__help">
  Инструменты серверов проходят Policy Engine: по умолчанию каждый вызов
  требует подтверждения.
</p>
```

- [ ] **Step 4: Дописать стили**

В `web/src/screens/McpScreen.css` добавить:

```css
.mcp__body {
  max-width: 1100px;
}

/* Пресеты: пустое состояние вкладки — предложение, а не констатация. */
.mcp__presets {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 8px;
  max-width: 820px;
  margin: 0 0 18px;
}
.mcp__preset {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 9px 12px;
  border: 0;
  border-radius: 9px;
  background: var(--surface);
  box-shadow: inset 0 0 0 1px var(--line);
  color: var(--text);
  font-family: var(--sans);
  text-align: left;
  cursor: pointer;
}
.mcp__preset:hover {
  background: var(--raised);
}
.mcp__preset-title {
  font-family: var(--mono);
  font-size: 12.5px;
}
.mcp__preset-hint {
  font-size: 11.5px;
  color: var(--faint);
}

.mcp__details-toggle {
  margin-bottom: 18px;
}
.mcp__details {
  max-width: 620px;
}

.mcp__risk-set {
  max-width: 620px;
  margin: 0 0 18px;
  padding: 0;
  border: 0;
}
.mcp__segments {
  display: flex;
  margin: 6px 0 8px;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: inset 0 0 0 1px var(--line);
}
.mcp__segment {
  flex: 1;
  padding: 7px 6px;
  color: var(--muted);
  font-family: var(--mono);
  font-size: 12.5px;
  text-align: center;
  cursor: pointer;
}
/* Радиокнопка спрятана визуально, но не от клавиатуры и не от скринридера:
   display:none выбил бы группу из Tab-навигации. */
.mcp__segment input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}
.mcp__segment:has(input:checked) {
  background: var(--raised);
  color: var(--text);
}
.mcp__segment:has(input:focus-visible) {
  outline: 2px solid var(--ember);
  outline-offset: -2px;
}

.mcp__result {
  max-width: 620px;
  margin-top: 14px;
}
.mcp__result-head {
  margin: 0 0 8px;
  font-size: 13.5px;
}
.mcp__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.mcp__chip {
  padding: 3px 9px;
  border-radius: 20px;
  background: var(--raised);
  color: var(--muted);
  font-family: var(--mono);
  font-size: 11.5px;
}
```

- [ ] **Step 5: Запустить тесты и убедиться, что они проходят**

Run: `npx vitest run src/screens/McpScreen.test.tsx`
Expected: PASS, 8 тестов. Если тест «клик по пресету» находит несколько кнопок с текстом `github` — значит в списке серверов уже есть такой сервер; в этом тесте `mcpList` возвращает `[]` по умолчанию `fakeApi`, поэтому совпадение должно быть одно.

- [ ] **Step 6: Коммит**

```bash
npm run format
npm test
git add src/screens/McpScreen.tsx src/screens/McpScreen.css src/screens/McpScreen.test.tsx
git commit -m "feat(web): подключение MCP одной строкой, каталог пресетов, риск с последствием"
```

---

### Task 5: Карточки подключённых серверов

Плоские строки становятся сеткой карточек; инструменты и живость запрашиваются по клику существующим `POST /mcp/test`.

**Files:**
- Modify: `web/src/screens/McpScreen.tsx` (блок списка серверов — строки 104-125 исходного файла)
- Modify: `web/src/screens/McpScreen.css`
- Modify: `web/src/screens/McpScreen.test.tsx` (добавить блок `describe`)

**Interfaces:**
- Consumes: `McpServer` из `web/src/api/types.ts:164-170`; `api.mcpTest`, `api.mcpRemove`; `riskClass`, `riskLabel` (задача 1).
- Produces: разметку карточки с кнопками `Инструменты` и `Удалить <имя>`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `web/src/screens/McpScreen.test.tsx`:

```tsx
describe("вкладка MCP: подключённые серверы", () => {
  const server = {
    name: "github",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-github"],
    env_refs: ["GITHUB_TOKEN"],
    risk: "high",
  };

  it("показывает карточку с командой, риском и секретами", async () => {
    const api = fakeApi({ mcpList: vi.fn().mockResolvedValue([server]) });
    render(<McpScreen api={api} />);
    await waitFor(() =>
      expect(screen.getByText("github")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("npx -y @modelcontextprotocol/server-github"),
    ).toBeInTheDocument();
    expect(screen.getByText("высокий риск")).toBeInTheDocument();
    expect(screen.getByText("GITHUB_TOKEN")).toBeInTheDocument();
  });

  it("«Инструменты» опрашивает именно этот сервер и рисует чипы", async () => {
    const api = fakeApi({
      mcpList: vi.fn().mockResolvedValue([server]),
      mcpTest: vi.fn().mockResolvedValue({
        ok: true,
        tools: ["create_issue", "search_code"],
        error: null,
      }),
    });
    render(<McpScreen api={api} />);
    await waitFor(() =>
      expect(screen.getByText("github")).toBeInTheDocument(),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Инструменты github" }),
    );
    await waitFor(() =>
      expect(screen.getByText("create_issue")).toBeInTheDocument(),
    );
    expect(api.mcpTest).toHaveBeenCalledWith({
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-github"],
      env_refs: ["GITHUB_TOKEN"],
    });
  });

  it("мёртвый сервер показывает ошибку, а не пустой список", async () => {
    const api = fakeApi({
      mcpList: vi.fn().mockResolvedValue([server]),
      mcpTest: vi
        .fn()
        .mockResolvedValue({ ok: false, tools: [], error: "npx не найден" }),
    });
    render(<McpScreen api={api} />);
    await waitFor(() =>
      expect(screen.getByText("github")).toBeInTheDocument(),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Инструменты github" }),
    );
    await waitFor(() =>
      expect(screen.getByText("npx не найден")).toBeInTheDocument(),
    );
  });

  it("не опрашивает серверы сама при открытии вкладки", async () => {
    const api = fakeApi({ mcpList: vi.fn().mockResolvedValue([server]) });
    render(<McpScreen api={api} />);
    await waitFor(() =>
      expect(screen.getByText("github")).toBeInTheDocument(),
    );
    expect(api.mcpTest).not.toHaveBeenCalled();
  });

  it("удаляет сервер после повторного клика", async () => {
    const api = fakeApi({ mcpList: vi.fn().mockResolvedValue([server]) });
    render(<McpScreen api={api} />);
    await waitFor(() =>
      expect(screen.getByText("github")).toBeInTheDocument(),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Удалить github" }),
    );
    expect(api.mcpRemove).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Точно удалить?" }));
    await waitFor(() => expect(api.mcpRemove).toHaveBeenCalledWith("github"));
  });
});
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `npx vitest run src/screens/McpScreen.test.tsx`
Expected: FAIL — `Unable to find role="button" and name "Инструменты github"`.

- [ ] **Step 3: Заменить список на карточки**

В `web/src/screens/McpScreen.tsx` добавить состояние:

```tsx
// Проверка живости — по клику, а не при открытии вкладки: автоопрос всех
// серверов означал бы запуск N процессов при каждом заходе.
const [probes, setProbes] = useState<Record<string, McpTest | "идёт">>({});
const [confirming, setConfirming] = useState<string | null>(null);

const probe = async (server: McpServer) => {
  setProbes((current) => ({ ...current, [server.name]: "идёт" }));
  try {
    const result = await api.mcpTest({
      command: server.command,
      args: server.args,
      env_refs: server.env_refs,
    });
    setProbes((current) => ({ ...current, [server.name]: result }));
  } catch (exc: unknown) {
    setProbes((current) => ({
      ...current,
      [server.name]: {
        ok: false,
        tools: [],
        error: exc instanceof ApiError ? exc.message : "проверка не удалась",
      },
    }));
  }
};

const remove = async (target: string) => {
  // Двухкликовое подтверждение вместо window.confirm: тестируемо и не
  // блокирует вкладку нативным диалогом (как у провайдеров).
  if (confirming !== target) {
    setConfirming(target);
    return;
  }
  setConfirming(null);
  try {
    await api.mcpRemove(target);
    setProbes((current) => {
      const next = { ...current };
      delete next[target];
      return next;
    });
    reload();
  } catch (exc: unknown) {
    setStatus(
      exc instanceof ApiError ? exc.message : "Не удалось удалить сервер.",
    );
  }
};
```

Разметку списка (строки 104-125 исходного файла) заменить на:

```tsx
{servers.length === 0 ? (
  <p className="field__help">
    Пока не подключено ни одного сервера — выберите готовый ниже или вставьте
    свою команду.
  </p>
) : (
  <div className="mcp__grid">
    {servers.map((server) => {
      const probed = probes[server.name];
      return (
        <div key={server.name} className="mcp__card">
          <div className="mcp__card-head">
            {probed !== undefined && probed !== "идёт" && (
              <span
                className={`mcp__dot${probed.ok ? "" : " mcp__dot--bad"}`}
                aria-hidden="true"
              />
            )}
            <span className="mcp__card-name">{server.name}</span>
            <span className={`mcp__card-risk ${riskClass(server.risk)}`}>
              {riskLabel(server.risk)}
            </span>
          </div>
          <div className="mcp__command">
            {[server.command, ...server.args].join(" ")}
          </div>
          {server.env_refs.length > 0 && (
            <div className="mcp__chips">
              {server.env_refs.map((ref) => (
                <span key={ref} className="mcp__chip">
                  {ref}
                </span>
              ))}
            </div>
          )}
          {probed === "идёт" && <p className="field__help">Опрашиваем…</p>}
          {probed !== undefined && probed !== "идёт" && (
            probed.ok ? (
              <div className="mcp__chips">
                {probed.tools.map((tool) => (
                  <span key={tool} className="mcp__chip">
                    {tool}
                  </span>
                ))}
              </div>
            ) : (
              <p className="field__error">{probed.error}</p>
            )
          )}
          <div className="mcp__card-actions">
            <button
              type="button"
              className="btn btn--small"
              aria-label={`Инструменты ${server.name}`}
              onClick={() => void probe(server)}
            >
              Инструменты
            </button>
            <button
              type="button"
              className="btn btn--small"
              aria-label={
                confirming === server.name
                  ? "Точно удалить?"
                  : `Удалить ${server.name}`
              }
              onClick={() => void remove(server.name)}
            >
              {confirming === server.name ? "Точно удалить?" : "Удалить"}
            </button>
          </div>
        </div>
      );
    })}
  </div>
)}
```

Добавить в импорты `riskClass, riskLabel` из `../model/risk` (к уже добавленным в задаче 4) и `type McpServer` — он уже импортирован строкой 4.

- [ ] **Step 4: Дописать стили карточек**

В `web/src/screens/McpScreen.css` добавить:

```css
.mcp__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
  margin: 12px 0 26px;
}
.mcp__card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  border-radius: 11px;
  background: var(--surface);
  box-shadow: inset 0 0 0 1px var(--line);
}
.mcp__card-head {
  display: flex;
  align-items: center;
  gap: 7px;
}
.mcp__card-name {
  font-size: 14px;
}
.mcp__card-risk {
  margin-left: auto;
  font-size: 11.5px;
}
.mcp__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ok);
}
.mcp__dot--bad {
  background: var(--bad);
}
.mcp__command {
  overflow: hidden;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mcp__card-actions {
  display: flex;
  gap: 6px;
  margin-top: auto;
  padding-top: 4px;
}
```

Удалить осиротевшие правила `.mcp__risk` и `.mcp__remove` (строки 4-13 исходного файла) — они относились к старой разметке `.secret`.

- [ ] **Step 5: Запустить тесты и убедиться, что они проходят**

Run: `npx vitest run src/screens/McpScreen.test.tsx`
Expected: PASS, 13 тестов (8 из задачи 4 плюс 5 новых).

- [ ] **Step 6: Коммит**

```bash
npm run format
npm test
git add src/screens/McpScreen.tsx src/screens/McpScreen.css src/screens/McpScreen.test.tsx
git commit -m "feat(web): подключённые MCP-серверы карточками с опросом инструментов"
```

---

### Task 6: Дифф-полоса вместо постоянной колонки

Убирает третью колонку и выключенный оранжевый CTA; центрирует контент.

**Files:**
- Modify: `web/src/screens/SettingsScreen.tsx:86-141` (компонент `DiffPane`), `751-955` (раскладка)
- Modify: `web/src/screens/SettingsScreen.css:100-231`
- Modify: `web/src/screens/SettingsScreen.test.tsx`

**Interfaces:**
- Consumes: `DiffLine`, `ConfigView` из `web/src/api/types.ts`; `counted` из `web/src/model/plural.ts`.
- Produces: `DiffBar` вместо `DiffPane` — сигнатура `{ path, lines, changes, error, open, onToggle, onSave, onReset }`; `data-testid="diffbar"`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `web/src/screens/SettingsScreen.test.tsx` внутрь существующего верхнего `describe`:

```tsx
it("при нуле изменений полосы сохранения нет вовсе", async () => {
  const api = baseApi({ config: vi.fn().mockResolvedValue(config) });
  render(<SettingsScreen api={api} />);
  await waitFor(() =>
    expect(screen.getByLabelText("Уровень автономии")).toBeInTheDocument(),
  );
  expect(screen.queryByTestId("diffbar")).not.toBeInTheDocument();
});

it("правка поля поднимает полосу с числом изменений", async () => {
  const api = baseApi({
    config: vi.fn().mockResolvedValue(config),
    previewConfig: vi.fn().mockResolvedValue({
      path: "/agent-home/svarog.yaml",
      lines: [{ kind: "add", text: "  autonomy: supervised" }],
      changes: 1,
      restart_required: false,
    }),
  });
  render(<SettingsScreen api={api} />);
  await waitFor(() =>
    expect(screen.getByLabelText("Уровень автономии")).toBeInTheDocument(),
  );
  await userEvent.selectOptions(
    screen.getByLabelText("Уровень автономии"),
    "supervised",
  );
  await waitFor(() =>
    expect(screen.getByTestId("diffbar")).toHaveTextContent("1 изменение"),
  );
});

it("дифф раскрывается по кнопке, а не занимает место постоянно", async () => {
  const api = baseApi({
    config: vi.fn().mockResolvedValue(config),
    previewConfig: vi.fn().mockResolvedValue({
      path: "/agent-home/svarog.yaml",
      lines: [{ kind: "add", text: "  autonomy: supervised" }],
      changes: 1,
      restart_required: false,
    }),
  });
  render(<SettingsScreen api={api} />);
  await waitFor(() =>
    expect(screen.getByLabelText("Уровень автономии")).toBeInTheDocument(),
  );
  await userEvent.selectOptions(
    screen.getByLabelText("Уровень автономии"),
    "supervised",
  );
  await waitFor(() => expect(screen.getByTestId("diffbar")).toBeInTheDocument());
  expect(screen.queryByText(/autonomy: supervised/)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Показать дифф" }));
  expect(screen.getByText(/autonomy: supervised/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `npx vitest run src/screens/SettingsScreen.test.tsx`
Expected: FAIL — `diffbar` не найден; существующие тесты диффа тоже могут упасть, если ищут `diffpane`.

- [ ] **Step 3: Заменить DiffPane на DiffBar**

В `web/src/screens/SettingsScreen.tsx` заменить компонент `DiffPane` (строки 86-141) на:

```tsx
/** Полоса сохранения: при нуле изменений её нет вовсе — постоянная колонка
    держала 344px пустоты и выключенный оранжевый CTA рядом с «0 изменений». */
function DiffBar({
  path,
  lines,
  changes,
  error,
  open,
  onToggle,
  onSave,
  onReset,
}: {
  path: string;
  lines: DiffLine[];
  changes: number;
  error: string | null;
  open: boolean;
  onToggle: () => void;
  onSave: () => void;
  onReset: () => void;
}) {
  return (
    <aside className="diffbar" data-open={open} data-testid="diffbar">
      {open && (
        <pre className="diffbar__body">
          {lines.map((line, index) => {
            // Знак и текст — одна строка: так она копируется целиком и
            // читается как настоящий дифф.
            const sign =
              line.kind === "add" ? "+" : line.kind === "del" ? "-" : " ";
            return (
              <span key={index} className={`diffpane__line--${line.kind}`}>
                {`${sign}${line.text}`}
              </span>
            );
          })}
        </pre>
      )}
      <div className="diffbar__foot">
        <span className="diffbar__count">
          {error !== null
            ? "изменения не пройдут проверку"
            : counted(changes, "изменение", "изменения", "изменений")}
        </span>
        <span className="diffbar__file">{path}</span>
        <button type="button" className="btn btn--small" onClick={onToggle}>
          {open ? "Скрыть дифф" : "Показать дифф"}
        </button>
        <button
          type="button"
          className="btn btn--primary"
          onClick={onSave}
          disabled={changes === 0 || error !== null}
        >
          Сохранить
        </button>
        <button type="button" className="btn" onClick={onReset}>
          Отменить
        </button>
      </div>
    </aside>
  );
}
```

В `SettingsScreen` заменить блок строк 928-952 на:

```tsx
{pane.kind === "section" && (changes > 0 || error !== null) && (
  <DiffBar
    path={config.path}
    lines={diff}
    changes={changes}
    error={error}
    open={sheetOpen}
    onToggle={() => setSheetOpen(!sheetOpen)}
    onSave={() => void save()}
    onReset={() => {
      setEdits({});
      setSheetOpen(false);
    }}
  />
)}
```

Обернуть содержимое `.settings__body` (строка 868) в колонку с ограничением ширины: заменить `<div className="settings__body">` на

```tsx
<div className="settings__body">
  <div className="settings__col">
```

и закрыть вторым `</div>` перед закрывающим тегом body.

- [ ] **Step 4: Переписать стили**

В `web/src/screens/SettingsScreen.css` заменить блок `.diffpane*` (строки 100-153) и мобильный блок (строки 195-231) на:

```css
/* Раскладка: две колонки, не три. Дифф приходит снизу и только когда есть
   что сохранять. */
.settings__body {
  position: relative;
  display: flex;
  flex-direction: column;
}
.settings__col {
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
}

.diffbar {
  position: sticky;
  z-index: 6;
  bottom: 0;
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--line-soft);
  background: var(--surface);
}
.diffbar__body {
  max-height: 40vh;
  margin: 0;
  padding: 12px 16px;
  overflow: auto;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.75;
  color: var(--muted);
  white-space: pre;
}
.diffpane__line--add {
  display: block;
  background: rgba(110, 155, 114, 0.11);
  color: #9dcba1;
}
.diffpane__line--del {
  display: block;
  background: rgba(196, 99, 92, 0.1);
  color: #dfa09b;
}
.diffpane__line--same {
  display: block;
}
.diffbar__foot {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 11px 16px;
}
.diffbar__count {
  font-size: 12.5px;
  color: var(--muted);
}
.diffbar__file {
  flex: 1;
  overflow: hidden;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--git);
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 899px) {
  .settings__nav {
    display: none;
  }
  .settings__body {
    padding: 18px 14px 0;
  }
  .diffbar__file {
    display: none;
  }
  .diffbar__foot .btn {
    min-height: 44px;
  }
}
```

Из `SettingsScreen.tsx` удалить блок `.settings__sheet-button` (строки 930-938) — кнопка «Показать изменения (N)» существовала только ради узкого экрана и заменена полосой; из CSS удалить правила `.settings__sheet-button` (строки 196-198).

- [ ] **Step 5: Починить существующие тесты диффа**

Существующие тесты `показывает дифф файла после правки и не сохраняет сам` (строка 98) и `сохраняет только по нажатию и сообщает число изменений` (строка 127) ищут дифф без раскрытия. Добавить в каждый перед проверкой строк диффа:

```tsx
await userEvent.click(screen.getByRole("button", { name: "Показать дифф" }));
```

- [ ] **Step 6: Запустить тесты и убедиться, что они проходят**

Run: `npx vitest run src/screens/SettingsScreen.test.tsx`
Expected: PASS, все тесты файла (22 существующих + 3 новых).

- [ ] **Step 7: Коммит**

```bash
npm run format
npm test
git add src/screens/SettingsScreen.tsx src/screens/SettingsScreen.css src/screens/SettingsScreen.test.tsx
git commit -m "feat(web): дифф настроек — полоса снизу вместо постоянной колонки"
```

---

### Task 7: Провайдер карточкой с правкой на месте

Убирает строку с шестью кнопками и телепорт к форме внизу.

**Files:**
- Modify: `web/src/screens/SettingsScreen.tsx:203-612` (компонент `ProvidersPane`)
- Modify: `web/src/screens/SettingsScreen.css:233-251`
- Modify: `web/src/screens/SettingsScreen.test.tsx:427-444` (тест «Изменить» заполняет форму)

**Interfaces:**
- Consumes: `ProviderCard` из `web/src/api/types.ts:186-191`; `api.providers`, `api.addProvider`, `api.providerCheck`, `api.providerRename`, `api.providerRemove`, `api.providerModels`.
- Produces: разметку карточки с кнопкой `Ещё <имя>` (раскрывает блок действий) и формой правки внутри карточки.

- [ ] **Step 1: Написать падающие тесты**

Заменить существующий тест `«Изменить» заполняет форму значениями провайдера` (строки 427-444) на:

```tsx
it("«Изменить» правит провайдера в его же карточке, не трогая форму добавления", async () => {
  const api = baseApi({
    config: vi.fn().mockResolvedValue(config),
    providers: vi.fn().mockResolvedValue([
      {
        name: "groq",
        base_url: "https://api.groq.com/openai/v1",
        model: "llama-3.3-70b",
        is_default: false,
      },
    ]),
  });
  render(<SettingsScreen api={api} />);
  await userEvent.click(screen.getByRole("button", { name: "Провайдеры" }));
  await waitFor(() => expect(screen.getByText("groq")).toBeInTheDocument());

  await userEvent.click(screen.getByRole("button", { name: "Ещё groq" }));
  await userEvent.click(screen.getByRole("button", { name: "Изменить" }));

  // Форма добавления осталась пустой — контекст не уехал вниз экрана.
  expect(screen.getByLabelText("Имя")).toHaveValue("");
  expect(screen.getByLabelText("Модель groq")).toHaveValue("llama-3.3-70b");

  await userEvent.clear(screen.getByLabelText("Модель groq"));
  await userEvent.type(screen.getByLabelText("Модель groq"), "llama-3.1-8b");
  await userEvent.click(screen.getByRole("button", { name: "Сохранить groq" }));
  await waitFor(() =>
    expect(api.addProvider).toHaveBeenCalledWith({
      name: "groq",
      base_url: "https://api.groq.com/openai/v1",
      model: "llama-3.1-8b",
    }),
  );
});

it("редкие действия провайдера спрятаны за «Ещё»", async () => {
  const api = baseApi({
    config: vi.fn().mockResolvedValue(config),
    providers: vi.fn().mockResolvedValue([
      {
        name: "groq",
        base_url: "https://api.groq.com/openai/v1",
        model: "llama-3.3-70b",
        is_default: false,
      },
    ]),
  });
  render(<SettingsScreen api={api} />);
  await userEvent.click(screen.getByRole("button", { name: "Провайдеры" }));
  await waitFor(() => expect(screen.getByText("groq")).toBeInTheDocument());

  expect(
    screen.queryByRole("button", { name: "Переименовать" }),
  ).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Ещё groq" }));
  expect(
    screen.getByRole("button", { name: "Переименовать" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Проверить" })).toBeInTheDocument();
});
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `npx vitest run src/screens/SettingsScreen.test.tsx -t "Ещё"`
Expected: FAIL — кнопка `Ещё groq` не найдена.

- [ ] **Step 3: Перестроить карточку провайдера**

В `ProvidersPane` заменить состояние `startEdit`-телепорта на состояние правки в карточке. Удалить функцию `startEdit` (строки 261-269) и добавить:

```tsx
const [expanded, setExpanded] = useState<string | null>(null);
const [editing, setEditing] = useState<{
  name: string;
  baseUrl: string;
  model: string;
  apiKey: string;
} | null>(null);

// Правка существующего идёт тем же addProvider, что и добавление: бэкенд
// различает их по имени, отдельного эндпоинта нет.
const submitEdit = async () => {
  if (editing === null) return;
  setStatus(null);
  try {
    const diff = await api.addProvider({
      name: editing.name,
      base_url: editing.baseUrl.trim(),
      model: editing.model.trim(),
      ...(editing.apiKey.trim() ? { api_key: editing.apiKey.trim() } : {}),
    });
    applied(diff, `Провайдер «${editing.name}» обновлён.`);
    setEditing(null);
    setOpenCatalog(null);
    setCatalogs({});
    reload();
  } catch (exc: unknown) {
    setStatus(
      exc instanceof ApiError ? exc.message : "Не удалось сохранить провайдера.",
    );
  }
};
```

Блок действий в карточке (заменяет `<span className="provider__actions">`, строки 455-504):

```tsx
<span className="provider__actions">
  {!card.is_default && (
    <button
      type="button"
      className="btn btn--small"
      onClick={() => void makeDefault(card.name)}
    >
      По умолчанию
    </button>
  )}
  <button
    type="button"
    className="btn btn--small"
    onClick={() => toggleCatalog(card.name)}
  >
    {openCatalog === card.name ? "Скрыть модели" : "Модели"}
  </button>
  <button
    type="button"
    className="btn btn--small"
    aria-label={`Ещё ${card.name}`}
    aria-expanded={expanded === card.name}
    onClick={() =>
      setExpanded(expanded === card.name ? null : card.name)
    }
  >
    ⋯
  </button>
</span>
```

Раскрываемый блок редких действий — сразу после `.secret`, внутри `.provider`:

```tsx
{expanded === card.name && (
  <div className="provider__more">
    <button
      type="button"
      className="btn btn--small"
      onClick={() => void runCheck(card.name)}
    >
      Проверить
    </button>
    <button
      type="button"
      className="btn btn--small"
      onClick={() =>
        setEditing({
          name: card.name,
          baseUrl: card.base_url,
          model: card.model,
          apiKey: "",
        })
      }
    >
      Изменить
    </button>
    <button
      type="button"
      className="btn btn--small"
      onClick={() => setRenaming({ name: card.name, value: card.name })}
    >
      Переименовать
    </button>
    {!card.is_default && (
      <button
        type="button"
        className="btn btn--small"
        onClick={() => void remove(card.name)}
      >
        {confirming === card.name ? "Точно удалить?" : "Удалить"}
      </button>
    )}
  </div>
)}

{editing !== null && editing.name === card.name && (
  <div className="provider__edit">
    <div className="field">
      <label className="field__label" htmlFor={`edit-url-${card.name}`}>
        Base URL {card.name}
      </label>
      <input
        id={`edit-url-${card.name}`}
        className="field__control"
        value={editing.baseUrl}
        onChange={(e) => setEditing({ ...editing, baseUrl: e.target.value })}
      />
    </div>
    <div className="field">
      <label className="field__label" htmlFor={`edit-model-${card.name}`}>
        Модель {card.name}
      </label>
      <input
        id={`edit-model-${card.name}`}
        className="field__control"
        value={editing.model}
        onChange={(e) => setEditing({ ...editing, model: e.target.value })}
      />
    </div>
    <div className="field">
      <label className="field__label" htmlFor={`edit-key-${card.name}`}>
        API-ключ {card.name}
      </label>
      <p className="field__help">Пустой ключ не меняется.</p>
      <input
        id={`edit-key-${card.name}`}
        className="field__control"
        type="password"
        value={editing.apiKey}
        onChange={(e) => setEditing({ ...editing, apiKey: e.target.value })}
      />
    </div>
    <div className="provider__more">
      <button
        type="button"
        className="btn btn--small"
        aria-label={`Сохранить ${card.name}`}
        onClick={() => void submitEdit()}
      >
        Сохранить
      </button>
      <button
        type="button"
        className="btn btn--small"
        onClick={() => setEditing(null)}
      >
        Отмена
      </button>
    </div>
  </div>
)}
```

Заголовок формы внизу (строка 534) меняется с `Добавить / обновить` на `Добавить провайдера`.

- [ ] **Step 4: Дописать стили**

В `web/src/screens/SettingsScreen.css` добавить после блока `.provider`:

```css
/* Раскрывающийся блок, не поповер: поповер требует управления фокусом,
   Escape и клика вне — цена, которую незачем платить ради четырёх кнопок. */
.provider__more {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 9px 0;
}
.provider__edit {
  margin: 4px 0 14px;
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--surface);
  box-shadow: inset 0 0 0 1px var(--line);
}
.provider__edit .field {
  margin-bottom: 14px;
}
```

- [ ] **Step 5: Запустить тесты и убедиться, что они проходят**

Run: `npx vitest run src/screens/SettingsScreen.test.tsx`
Expected: PASS. Тесты `проверяет доступность провайдера из строки` (строка 335), `переименовывает провайдера через инлайн-поле` (365), `отправляет инлайн-переименование по Enter` (388) и `удаляет провайдера после повторного клика` (408) теперь требуют предварительного клика по `Ещё <имя>` — добавить его в начало каждого:

```tsx
await userEvent.click(screen.getByRole("button", { name: "Ещё groq" }));
```

(имя провайдера в каждом тесте своё — взять из его мока `providers`).

- [ ] **Step 6: Коммит**

```bash
npm run format
npm test
git add src/screens/SettingsScreen.tsx src/screens/SettingsScreen.css src/screens/SettingsScreen.test.tsx
git commit -m "feat(web): провайдер — карточка с правкой на месте вместо шести кнопок"
```

---

### Task 8: Проверка в браузере и сборка

Плана без живой проверки недостаточно: тесты не ловят раскладку.

**Files:**
- Modify: ничего по умолчанию (правки — по результатам проверки)

**Interfaces:**
- Consumes: всё из задач 1-7.
- Produces: подтверждение, что обе вкладки собираются и выглядят как задумано.

- [ ] **Step 1: Собрать проект**

Run: `npm run build`
Expected: успех, без ошибок `tsc`.

- [ ] **Step 2: Поднять дев-сервер и открыть вкладки**

Поднять превью (`preview_start` с конфигурацией из `.claude/launch.json`; если файла нет — создать запись с `npm run dev` и портом Vite 5173). Открыть вкладку MCP и вкладку Настроек.

- [ ] **Step 3: Проверить раскладку**

Проверить на ширине 1440 и 900:
- MCP: пресеты видны при пустой форме и исчезают после вставки; карточки серверов идут сеткой и занимают ширину; чипы инструментов переносятся, а не режутся.
- Настройки: при нуле изменений внизу пусто; после правки поля появляется полоса; «Показать дифф» раскрывает панель и не выталкивает контент за экран.
- Консоль браузера без ошибок (`read_console_messages`).

- [ ] **Step 4: Проверить клавиатуру**

Пройти Tab по сегментам риска на вкладке MCP: фокус виден, стрелки переключают уровень (поведение радиогруппы по умолчанию). Если фокус невидим — проверить правило `.mcp__segment:has(input:focus-visible)` из задачи 4.

- [ ] **Step 5: Снять скриншоты обеих вкладок**

Скриншоты приложить к отчёту — они и есть доказательство, что раскладка починена.

- [ ] **Step 6: Финальная проверка и коммит правок**

```bash
npm run format
npm test
```

Если по результатам проверки были правки:

```bash
git add -A
git commit -m "fix(web): правки раскладки MCP и настроек по итогам живой проверки"
```

---

## Замечания для исполнителя

**Порядок обязателен для задач 1-3** (модули) перед 4-5 (экран MCP): задачи 4 и 5 импортируют то, что создают 1-3. Задачи 6 и 7 независимы от 1-5 и могут идти параллельно с ними, но обе трогают `SettingsScreen.tsx` и `SettingsScreen.test.tsx` — между собой их надо выполнять последовательно.

**`prettier --check` входит в `npm test`.** Забытый `npm run format` даёт падение, которое легко принять за содержательную ошибку.

**Селектор `:has()`** используется в задаче 4 (`.mcp__segment:has(input:checked)`). Поддерживается всеми целевыми браузерами; jsdom его не вычисляет, но тесты проверяют состояние радиокнопки, а не цвет, поэтому это не мешает.

**Чего этот план не делает** (граница из спеки): единой модели сохранения для провайдеров и исполнителей, поиска по настройкам, редактируемого yaml. Бэкенд, схема `svarog.yaml` и контракты API не трогаются ни в одной задаче.
