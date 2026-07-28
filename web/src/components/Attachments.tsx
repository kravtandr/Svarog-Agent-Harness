import type { Attachment } from "../api/types";
import "./Attachments.css";

/**
 * Значок картинки или документа — превью здесь не рисуем: до отправки файл
 * существует только на сервере, а миниатюра появится в ленте уже после
 * отправки, когда путь вложения известен сообщению.
 */
function icon(mime: string | null): string {
  return mime !== null && mime.startsWith("image/") ? "🖼" : "📄";
}

export function Attachments({
  items,
  onRemove,
}: {
  items: Attachment[];
  onRemove: (path: string) => void;
}) {
  // Пустой список не рисуем вовсе: полоса без чипов читается как поломка.
  if (items.length === 0) return null;

  return (
    <ul className="attachments" aria-label="Вложения">
      {items.map((item) => (
        <li key={item.path} className="attachments__chip">
          <span className="attachments__icon" aria-hidden="true">
            {icon(item.mime)}
          </span>
          {/* Имя из ответа сервера, а не путь на диске: путь несёт хэш-префикс
              против коллизий имён — деталь реализации, не для человека. */}
          <span className="attachments__name">{item.name}</span>
          <button
            type="button"
            className="attachments__remove"
            aria-label={`Убрать ${item.name}`}
            onClick={() => onRemove(item.path)}
          >
            ×
          </button>
          {item.too_large_for_vision && (
            <p className="attachments__warning">
              файл больше 5 MB, модель не увидит его целиком; пригодится как
              исходник
            </p>
          )}
        </li>
      ))}
    </ul>
  );
}
