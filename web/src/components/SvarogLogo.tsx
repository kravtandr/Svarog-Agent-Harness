import { useId } from "react";

/**
 * Пиксельный логотип SVAROG для шапки навигатора (выбор 05.08.2026,
 * вариант «белый по углям»): белые буквы в три полосы + смещённый
 * двойной контур цвета ember сзади.
 *
 * Буквы нарисованы на сетке 6×8 и обведены в один SVG-path заранее —
 * в рантайме ни шрифта, ни генерации. Дырки букв вычитаются правилом
 * nonzero: внешние петли обведены по часовой, внутренние — против.
 */
const WORD =
  "M0 0L6 0L6 2L2 2L2 3L6 3L6 8L0 8L0 6L4 6L4 5L0 5ZM7.5 0L9.5 0L9.5 5" +
  "L11.5 5L11.5 0L13.5 0L13.5 5L12.5 5L12.5 6L11.5 6L11.5 8L9.5 8L9.5 6" +
  "L8.5 6L8.5 5L7.5 5ZM16 0L20 0L20 1L21 1L21 8L19 8L19 5L17 5L17 8L15 8" +
  "L15 1L16 1ZM17 2L17 3L19 3L19 2ZM22.5 0L28.5 0L28.5 5L27.5 5L27.5 6" +
  "L28.5 6L28.5 8L26.5 8L26.5 6L25.5 6L25.5 5L24.5 5L24.5 8L22.5 8" +
  "ZM24.5 2L24.5 3L26.5 3L26.5 2ZM30 0L36 0L36 8L30 8ZM32 2L32 6L34 6" +
  "L34 2ZM37.5 0L43.5 0L43.5 2L39.5 2L39.5 6L41.5 6L41.5 5L40.5 5L40.5 4" +
  "L43.5 4L43.5 8L37.5 8Z";

/* Полосы режут все буквы по одним и тем же строкам сетки (3/2/3 из
   восьми): сверху чистый белый, дальше тона из tokens.css. */
const BANDS = [
  { y: 0, h: 3, fill: "#ffffff" },
  { y: 3, h: 2, fill: "var(--text)" },
  { y: 5, h: 3, fill: "#c9c2b6" },
];

export function SvarogLogo({ height = 18 }: { height?: number }) {
  const uid = useId();
  return (
    <svg
      viewBox="-2 -2 47.5 12.5"
      height={height}
      role="img"
      aria-label="Сварог"
    >
      <defs>
        {BANDS.map((band, i) => (
          <clipPath key={band.y} id={`${uid}b${i}`}>
            <rect x="-2" y={band.y} width="47.5" height={band.h} />
          </clipPath>
        ))}
      </defs>
      {/* Контур-«тень»: тонкий штрих фоном поверх толстого ember-штриха
          распускает его на две параллельные линии, как в референсе. */}
      <g transform="translate(1 1.2)">
        <path d={WORD} fill="none" stroke="var(--ember)" strokeWidth="0.55" />
        <path d={WORD} fill="none" stroke="var(--bg)" strokeWidth="0.2" />
      </g>
      {BANDS.map((band, i) => (
        <g key={band.y} clipPath={`url(#${uid}b${i})`}>
          <path d={WORD} fill={band.fill} />
        </g>
      ))}
    </svg>
  );
}
