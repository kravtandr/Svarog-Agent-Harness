import type { RunRef, SessionSummary, SessionThread } from './types'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export interface ClientOptions {
  baseUrl: string
  token?: string
}

export interface Api {
  listSessions(): Promise<SessionSummary[]>
  sessionThread(sessionId: string): Promise<SessionThread>
  createSession(title: string): Promise<{ session_id: string }>
  sendMessage(sessionId: string, text: string): Promise<RunRef>
  decideApproval(approvalId: string, approved: boolean): Promise<RunRef>
}

export function createClient({ baseUrl, token }: ClientOptions): Api {
  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = { 'content-type': 'application/json' }
    if (token) headers.Authorization = `Bearer ${token}`
    const response = await fetch(`${baseUrl}${path}`, { ...init, headers })
    if (!response.ok) {
      // detail — стандартная форма ошибки FastAPI; без него берём статус.
      const body: unknown = await response.json().catch(() => null)
      const detail =
        body !== null &&
        typeof body === 'object' &&
        'detail' in body &&
        typeof body.detail === 'string'
          ? body.detail
          : response.statusText
      throw new ApiError(response.status, detail)
    }
    return (await response.json()) as T
  }

  return {
    listSessions: () => request<SessionSummary[]>('/sessions'),
    sessionThread: (sessionId) =>
      request<SessionThread>(`/sessions/${encodeURIComponent(sessionId)}/messages`),
    createSession: (title) =>
      request<{ session_id: string }>('/sessions', {
        method: 'POST',
        body: JSON.stringify({ title }),
      }),
    sendMessage: (sessionId, text) =>
      request<RunRef>(`/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: 'POST',
        body: JSON.stringify({ text }),
      }),
    decideApproval: (approvalId, approved) =>
      request<RunRef>(`/approvals/${encodeURIComponent(approvalId)}`, {
        method: 'POST',
        body: JSON.stringify({ approved }),
      }),
  }
}
