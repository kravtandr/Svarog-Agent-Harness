import { useState } from 'react'

import './Composer.css'

export function Composer({
  onSend,
  autonomy,
  executor,
  model,
}: {
  onSend: (text: string) => void
  autonomy: string
  executor: string
  model: string
}) {
  const [text, setText] = useState('')

  function send() {
    const trimmed = text.trim()
    if (!trimmed) return
    onSend(trimmed)
    setText('')
  }

  return (
    <div className="composer">
      <div className="composer__inner">
        <div className="composer__box">
          <textarea
            className="composer__field"
            aria-label="Написать Сварогу"
            placeholder="Написать Сварогу"
            rows={1}
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          <div className="composer__foot">
            {/* Режимы стоят там, где на них смотрят перед отправкой. */}
            <span>{autonomy}</span>
            <span>{executor}</span>
            <span>{model}</span>
            <span className="composer__spacer" />
            {/* Место под голос занято сразу: включение не потребует переверстки. */}
            <button
              type="button"
              className="composer__icon"
              aria-label="Голосовой ввод"
              aria-describedby="mic-hint"
              disabled
            >
              ●
            </button>
            <span id="mic-hint" className="composer__hidden">
              Голосовой ввод появится позже
            </span>
            <button
              type="button"
              className="composer__icon composer__icon--send"
              aria-label="Отправить"
              onClick={send}
            >
              ↑
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
