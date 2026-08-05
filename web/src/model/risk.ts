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
