import DOMPurify from "dompurify";
import { marked } from "marked";
import { useMemo } from "react";

import "./Markdown.css";

/** Ответы агента — почти всегда markdown (заголовки, таблицы, код): сырой
    текст с решётками и пайпами нечитаем. marked парсит (GFM — таблицы),
    DOMPurify срезает всё исполняемое: текст приходит от LLM, то есть
    недоверенный — dangerouslySetInnerHTML без санитайзера был бы XSS через
    ответ модели. */
export function Markdown({ text }: { text: string }) {
  const html = useMemo(
    () =>
      DOMPurify.sanitize(
        marked.parse(text, { async: false, gfm: true, breaks: true }),
        // Ссылки оставляем, но target/rel навешиваем ниже хуком — без
        // target=_blank клик уводил бы из чата, теряя живой стрим.
        { FORBID_TAGS: ["style"], FORBID_ATTR: ["onerror", "onload"] },
      ),
    [text],
  );
  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}

// Все ссылки из ответа — в новой вкладке и без opener'а: чат с живым
// WS-стримом не должен умирать от клика по ссылке из ответа модели.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});
