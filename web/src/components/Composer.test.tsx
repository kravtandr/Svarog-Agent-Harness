import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Composer } from './Composer'

const props = { autonomy: 'под надзором', executor: 'нативный цикл', model: 'qwen3-coder' }

describe('поле ввода', () => {
  it('отправляет текст и очищает поле', async () => {
    const onSend = vi.fn()
    render(<Composer {...props} onSend={onSend} />)

    const field = screen.getByRole('textbox', { name: /написать/i })
    await userEvent.type(field, 'прогони тесты')
    await userEvent.click(screen.getByRole('button', { name: 'Отправить' }))

    expect(onSend).toHaveBeenCalledWith('прогони тесты')
    expect(field).toHaveValue('')
  })

  it('не отправляет пустое', async () => {
    const onSend = vi.fn()
    render(<Composer {...props} onSend={onSend} />)
    await userEvent.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(onSend).not.toHaveBeenCalled()
  })

  it('показывает режимы под строкой', () => {
    render(<Composer {...props} onSend={() => {}} />)
    expect(screen.getByText(/под надзором/)).toBeInTheDocument()
    expect(screen.getByText(/нативный цикл/)).toBeInTheDocument()
    expect(screen.getByText(/qwen3-coder/)).toBeInTheDocument()
  })

  it('держит место под микрофон выключенной кнопкой', () => {
    render(<Composer {...props} onSend={() => {}} />)
    const mic = screen.getByRole('button', { name: /голосовой ввод/i })
    expect(mic).toBeDisabled()
    expect(mic).toHaveAccessibleDescription(/появится позже/i)
  })
})
