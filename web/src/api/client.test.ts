import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, createClient } from './client'

describe('клиент API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('подставляет bearer и базовый URL', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const api = createClient({ baseUrl: 'http://svarog.test', token: 'секрет' })
    await api.listSessions()

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://svarog.test/sessions')
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer секрет')
  })

  it('не шлёт заголовок авторизации без токена', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('[]', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await createClient({ baseUrl: '' }).listSessions()

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined()
  })

  it('превращает ошибку сервера в ApiError с текстом detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ detail: 'нет такой сессии' }), { status: 404 }),
        ),
    )

    const api = createClient({ baseUrl: '' })

    await expect(api.sessionThread('нет')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: 'нет такой сессии',
    })
    await expect(api.sessionThread('нет')).rejects.toBeInstanceOf(ApiError)
  })

  it('экранирует идентификатор сессии в пути', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await createClient({ baseUrl: '' }).sendMessage('a/b', 'привет')

    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('/sessions/a%2Fb/messages')
  })
})
