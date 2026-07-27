import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { Api } from '../api/client'
import { ChatScreen } from './ChatScreen'

function fakeApi(over: Partial<Api> = {}): Api {
  return {
    listSessions: vi.fn().mockResolvedValue([
      {
        session_id: 's1',
        title: 'FTS-поиск по памяти',
        workspace: null,
        updated_at: new Date().toISOString(),
        runs_count: 1,
        last_state: 'completed',
      },
    ]),
    sessionThread: vi.fn().mockResolvedValue({
      session_id: 's1',
      title: 'FTS-поиск по памяти',
      items: [
        {
          kind: 'user',
          text: 'Добавь FTS-поиск',
          server: null,
          name: '',
          arg: '',
          result: '',
          status: '',
        },
        {
          kind: 'call',
          text: '',
          server: null,
          name: 'write_file',
          arg: 'memory/index.py',
          result: 'записано 1234 символов',
          status: 'succeeded',
        },
      ],
    }),
    createSession: vi.fn().mockResolvedValue({ session_id: 's2' }),
    sendMessage: vi.fn().mockResolvedValue({ run_id: 'r1', state: 'running' }),
    decideApproval: vi.fn().mockResolvedValue({ run_id: 'r1', state: 'running' }),
    ...over,
  }
}

describe('экран диалога', () => {
  it('рисует историю выбранной сессии', async () => {
    render(<ChatScreen api={fakeApi()} />)

    await waitFor(() => expect(screen.getByText('Добавь FTS-поиск')).toBeInTheDocument())
    expect(screen.getByText('write_file')).toBeInTheDocument()
    expect(screen.getByText('записано 1234 символов')).toBeInTheDocument()
  })

  it('отправляет сообщение в текущую сессию', async () => {
    const api = fakeApi()
    render(<ChatScreen api={api} />)
    await waitFor(() => expect(screen.getByText('Добавь FTS-поиск')).toBeInTheDocument())

    await userEvent.type(screen.getByRole('textbox', { name: /написать/i }), 'прогони тесты')
    await userEvent.click(screen.getByRole('button', { name: 'Отправить' }))

    expect(api.sendMessage).toHaveBeenCalledWith('s1', 'прогони тесты')
    await waitFor(() => expect(screen.getByText('прогони тесты')).toBeInTheDocument())
  })

  it('показывает ошибку загрузки, а не пустой экран', async () => {
    const api = fakeApi({ listSessions: vi.fn().mockRejectedValue(new Error('нет связи')) })
    render(<ChatScreen api={api} />)
    await waitFor(() =>
      expect(screen.getByText(/не удалось загрузить сессии/i)).toBeInTheDocument(),
    )
  })

  it('пока грузится — говорит об этом, а не показывает пустоту', async () => {
    render(<ChatScreen api={fakeApi()} />)

    expect(screen.getByText(/загружаем/i)).toBeInTheDocument()

    // Дожидаемся конца загрузки: иначе состояние обновится после теста и
    // React пожалуется на изменение вне act(...).
    await waitFor(() => expect(screen.queryByText(/загружаем/i)).not.toBeInTheDocument())
  })

  it('пустая сессия приглашает к действию, а не сообщает «нет данных»', async () => {
    const api = fakeApi({
      sessionThread: vi
        .fn()
        .mockResolvedValue({ session_id: 's1', title: 'Новый чат', items: [] }),
    })
    render(<ChatScreen api={api} />)

    await waitFor(() => expect(screen.getByText(/поставьте задачу/i)).toBeInTheDocument())
    expect(screen.queryByText(/нет данных/i)).not.toBeInTheDocument()
  })
})
