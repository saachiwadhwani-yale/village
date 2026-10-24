// Thin wrappers around the backend routes (see backend/main.py).

import type { CareRequest, Message, PauseMode, TraceEvent, UsageSummary, User } from './types'

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* not JSON */
    }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return res.json() as Promise<T>
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

export const api = {
  users: () => call<User[]>('/users'),
  inbox: () => call<Record<string, number>>('/inbox'),
  messages: (userId: string, after = 0) =>
    call<{ messages: Message[]; typing: boolean }>(`/users/${userId}/messages?after=${after}`),
  send: (userId: string, text: string) => call<{ id: number }>(`/users/${userId}/messages`, json('POST', { text })),
  press: (userId: string, action: string, requestId: number | null, messageId: number, values: string[] = []) =>
    call(`/users/${userId}/buttons`, json('POST', { action, request_id: requestId, message_id: messageId, values })),
  newUser: (name: string, program: string) => call<User>('/users', json('POST', { name, program })),
  open: (userId: string) => call(`/users/${userId}/open`, { method: 'POST' }),
  restartSetup: (userId: string) => call(`/users/${userId}/setup/restart`, { method: 'POST' }),
  addKid: (userId: string, name: string, age: string, notes: string) =>
    call<User>(`/users/${userId}/kids`, json('POST', { name, age, notes })),
  removeKid: (userId: string, kidId: string) => call<User>(`/users/${userId}/kids/${kidId}`, { method: 'DELETE' }),
  setVillage: (userId: string, villagerIds: string[]) =>
    call<User>(`/users/${userId}/village`, json('PUT', { villager_ids: villagerIds })),
  addCalendar: (userId: string, label: string, url: string) =>
    call<User>(`/users/${userId}/calendars`, json('POST', { label, url })),
  removeCalendar: (userId: string, calId: number) =>
    call<User>(`/users/${userId}/calendars/${calId}`, { method: 'DELETE' }),
  leaveVillage: (userId: string, parentId: string) =>
    call<User>(`/users/${userId}/villages/${parentId}`, { method: 'DELETE' }),
  pause: (userId: string, mode: PauseMode) => call<User>(`/users/${userId}/pause`, json('PUT', { mode })),
  setAvailability: (userId: string, text: string) =>
    call<User>(`/users/${userId}/availability`, json('PUT', { text })),
  requests: () => call<CareRequest[]>('/requests'),
  events: (after = 0) => call<TraceEvent[]>(`/events?after=${after}`),
  usage: () => call<UsageSummary>('/usage'),
  reset: () => call('/reset', { method: 'POST' }),
}
