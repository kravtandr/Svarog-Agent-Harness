import { type ReactNode, useState } from 'react'

import './Shell.css'

export function Shell({
  nav,
  bar,
  children,
}: {
  nav: ReactNode
  bar: ReactNode
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)

  return (
    <div className="shell">
      <div className="shell__nav" data-testid="shell-nav" data-open={open}>
        {nav}
      </div>
      {/* Затемнение видно только когда панель выдвинута — правило в CSS. */}
      <div
        className="shell__scrim"
        data-testid="shell-scrim"
        onClick={() => setOpen(false)}
        aria-hidden="true"
      />
      <div className="shell__main">
        <div className="shell__bar">
          <button
            type="button"
            className="shell__burger"
            aria-label="Показать навигатор"
            onClick={() => setOpen((was) => !was)}
          >
            ☰
          </button>
          {bar}
        </div>
        {children}
      </div>
    </div>
  )
}
