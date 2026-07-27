import { useCallback, useEffect, useState } from "react";

import { createClient, type Api } from "./api/client";
import type { SessionSummary } from "./api/types";
import { Nav, type Section } from "./components/Nav";
import { Shell } from "./components/Shell";
import { ChatScreen } from "./screens/ChatScreen";
import { MemoryScreen } from "./screens/MemoryScreen";
import { RunsScreen } from "./screens/RunsScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { SkillsScreen } from "./screens/SkillsScreen";

/**
 * Токен берётся из `?token=` и запоминается в sessionStorage.
 *
 * `gateway.token_ref` обязателен для любого не-loopback bind, и без него
 * весь интерфейс получал бы 401. Ссылку с токеном печатает `svarog serve`.
 * sessionStorage, а не localStorage: токен не переживает закрытие вкладки.
 */
function readToken(): string | undefined {
  const fromUrl = new URLSearchParams(window.location.search).get("token");
  if (fromUrl) {
    sessionStorage.setItem("svarog-token", fromUrl);
    // Убираем токен из адресной строки: он не должен оседать в истории.
    window.history.replaceState({}, "", window.location.pathname);
    return fromUrl;
  }
  return sessionStorage.getItem("svarog-token") ?? undefined;
}

// Статика раздаётся тем же svarog serve, поэтому базовый URL пустой.
const token = typeof window === "undefined" ? undefined : readToken();
const defaultApi = createClient({ baseUrl: "", token });

const TITLES: Record<Section, string> = {
  chat: "Сварог",
  runs: "Запуски",
  skills: "Скиллы",
  memory: "Память",
  settings: "Настройки",
};

/**
 * Оболочка приложения: навигатор и переключение разделов.
 *
 * Сессии живут здесь, а не в экране диалога: их показывает навигатор,
 * который виден на всех разделах.
 */
export function App({ api = defaultApi }: { api?: Api } = {}) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [section, setSection] = useState<Section>("chat");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    const listed = await api.listSessions();
    setSessions(listed);
    setActiveId((current) => current ?? listed[0]?.session_id ?? null);
  }, [api]);

  useEffect(() => {
    reload()
      .catch(() =>
        setError(
          "Не удалось загрузить сессии. Проверьте, что svarog serve запущен.",
        ),
      )
      .finally(() => setLoading(false));
  }, [reload]);

  const startNew = useCallback(async () => {
    const created = await api.createSession("Новый чат");
    setActiveId(created.session_id);
    setSection("chat");
    await reload();
    return created.session_id;
  }, [api, reload]);

  /** Сессия для отправки: текущая, а если её нет — новая. */
  const ensureSession = useCallback(
    async () => activeId ?? (await startNew()),
    [activeId, startNew],
  );

  const active = sessions.find((session) => session.session_id === activeId);

  return (
    <Shell
      nav={
        <Nav
          sessions={sessions}
          activeId={activeId}
          onPick={(id) => {
            setActiveId(id);
            setSection("chat");
          }}
          onNew={() => void startNew()}
          section={section}
          onSection={setSection}
        />
      }
      bar={
        <span>
          {section === "chat"
            ? (active?.title ?? TITLES.chat)
            : TITLES[section]}
        </span>
      }
    >
      {section === "settings" && <SettingsScreen api={api} />}
      {section === "memory" && <MemoryScreen api={api} />}
      {section === "skills" && <SkillsScreen api={api} />}
      {section === "runs" && <RunsScreen api={api} />}
      {section === "chat" && (
        <ChatScreen
          api={api}
          sessionId={activeId}
          ensureSession={ensureSession}
          loading={loading}
          error={error}
          token={token}
        />
      )}
    </Shell>
  );
}
