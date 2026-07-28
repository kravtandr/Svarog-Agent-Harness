/**
 * Русское числительное: «1 скилл», «2 скилла», «5 скиллов».
 *
 * Без этого интерфейс пишет «1 скиллов» и «Найдено в 1 записях» — мелочь,
 * по которой сразу видно, что текст никто не вычитывал.
 */
export function plural(
  count: number,
  one: string,
  few: string,
  many: string,
): string {
  const abs = Math.abs(count) % 100;
  const last = abs % 10;
  if (abs > 10 && abs < 20) return many;
  if (last > 1 && last < 5) return few;
  if (last === 1) return one;
  return many;
}

/** Число вместе со склонённым словом: «3 записи». */
export function counted(
  count: number,
  one: string,
  few: string,
  many: string,
): string {
  return `${count} ${plural(count, one, few, many)}`;
}
