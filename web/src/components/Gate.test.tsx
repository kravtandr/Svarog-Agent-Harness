import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Gate } from './Gate'

const gate = {
  kind: 'gate' as const,
  id: 'g1',
  approvalId: 'ap-1',
  actionType: 'run_shell',
  command: 'uv run pytest tests/memory/ -q',
}

describe('гейт разрешения', () => {
  it('показывает команду и правило', () => {
    render(<Gate gate={gate} onDecide={() => {}} />)
    expect(screen.getByText('uv run pytest tests/memory/ -q')).toBeInTheDocument()
    expect(screen.getByText(/run_shell/)).toBeInTheDocument()
  })

  it('сообщает решение наверх', async () => {
    const onDecide = vi.fn()
    render(<Gate gate={gate} onDecide={onDecide} />)

    await userEvent.click(screen.getByRole('button', { name: 'Разрешить' }))
    expect(onDecide).toHaveBeenCalledWith(true)

    await userEvent.click(screen.getByRole('button', { name: 'Отклонить' }))
    expect(onDecide).toHaveBeenCalledWith(false)
  })

  it('ставит «Разрешить» первой кнопкой', () => {
    render(<Gate gate={gate} onDecide={() => {}} />)
    expect(screen.getAllByRole('button')[0]).toHaveTextContent('Разрешить')
  })
})
