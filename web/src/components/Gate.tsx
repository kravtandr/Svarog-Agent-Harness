import type { ThreadItem } from '../model/thread'
import './Gate.css'

type GateItem = Extract<ThreadItem, { kind: 'gate' }>

export function Gate({
  gate,
  onDecide,
}: {
  gate: GateItem
  onDecide: (approved: boolean) => void
}) {
  return (
    <div className="gate">
      <div className="gate__head">Команда не выполнится без вашего решения</div>
      <pre className="gate__cmd">{gate.command}</pre>
      <div className="gate__row">
        <button
          type="button"
          className="gate__btn gate__btn--primary"
          onClick={() => onDecide(true)}
        >
          Разрешить
        </button>
        <button type="button" className="gate__btn gate__btn--ghost" onClick={() => onDecide(false)}>
          Отклонить
        </button>
        {/* Правило, по которому агент остановился: решение принимается со знанием причины. */}
        <span className="gate__rule">{gate.actionType}</span>
      </div>
    </div>
  )
}
