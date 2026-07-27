import { useCallback, useEffect, useState } from "react";

import { ApiError, type Api } from "../api/client";
import type { MemoryFile, MemoryHit, MemoryPage } from "../api/types";
import { counted } from "../model/plural";
import "./MemoryScreen.css";

/**
 * Экран памяти: markdown-страницы Сварога как они лежат в Git.
 *
 * Поиск идёт через тот же search_memory, которым агент находит записи сам,
 * — если он чего-то не нашёл, это воспроизводится здесь.
 */
export function MemoryScreen({ api }: { api: Api }) {
  const [pages, setPages] = useState<MemoryPage[]>([]);
  const [hits, setHits] = useState<MemoryHit[] | null>(null);
  const [query, setQuery] = useState("");
  const [file, setFile] = useState<MemoryFile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .memoryTree()
      .then((tree) => {
        setPages(tree);
        return tree[0] === undefined
          ? null
          : api.memoryFile(tree[0].path).then(setFile);
      })
      .catch((exc: unknown) =>
        setError(
          exc instanceof ApiError && exc.status === 404
            ? "Память не настроена: в svarog.yaml нет memory.path."
            : "Не удалось загрузить память.",
        ),
      )
      .finally(() => setLoading(false));
  }, [api]);

  const open = useCallback(
    async (path: string) => {
      setFile(await api.memoryFile(path));
    },
    [api],
  );

  const search = useCallback(
    async (text: string) => {
      setQuery(text);
      if (text.trim() === "") {
        setHits(null);
        return;
      }
      setHits(await api.memorySearch(text));
    },
    [api],
  );

  if (loading) return <p className="memory__hint">Загружаем память…</p>;
  if (error !== null) return <p className="memory__error">{error}</p>;

  return (
    <div className="memory">
      <div className="memory__tree">
        <input
          className="memory__search"
          type="search"
          aria-label="Поиск по памяти"
          placeholder="Поиск по содержимому"
          value={query}
          onChange={(event) => void search(event.target.value)}
        />

        {hits !== null && (
          <>
            <div className="memory__group">
              {hits.length === 0
                ? "Ничего не найдено"
                : `Найдено в ${counted(hits.length, "записи", "записях", "записях")}`}
            </div>
            {hits.map((hit) => (
              <button
                key={hit.path}
                type="button"
                className="memory__page"
                onClick={() => void open(hit.path)}
              >
                <span className="memory__path">{hit.path}</span>
                <span className="memory__snippet">{hit.snippet}</span>
              </button>
            ))}
          </>
        )}

        <div className="memory__group">
          {counted(pages.length, "запись", "записи", "записей")}
        </div>
        {pages.map((page) => (
          <button
            key={page.path}
            type="button"
            className={`memory__page${file?.path === page.path ? " memory__page--active" : ""}`}
            onClick={() => void open(page.path)}
          >
            <span className="memory__path">{page.path}</span>
          </button>
        ))}
      </div>

      <div className="memory__doc">
        {file === null ? (
          <p className="memory__hint">
            Память пуста — Сварог запишет сюда первые решения после первой
            задачи.
          </p>
        ) : (
          <>
            <div className="memory__doc-path">memory/{file.path}</div>
            <div className="memory__doc-meta">
              <span>{new Date(file.modified_at).toLocaleString("ru-RU")}</span>
              <span>{file.size_bytes} байт</span>
            </div>
            {/* Markdown показывается как есть: память — обычный текст,
                и человек должен видеть ровно то, что лежит в Git. */}
            <pre className="memory__text">{file.text}</pre>
          </>
        )}
      </div>
    </div>
  );
}
