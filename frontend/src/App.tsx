import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import { PeopleGuide } from './components/PeopleGuide'
import { RequestPanel } from './components/RequestPanel'
import { SlackWindow } from './components/SlackWindow'
import type { CareRequest, TraceEvent, User } from './types'

export default function App() {
  const [showPanel, setShowPanel] = useState(true)
  const [tab, setTab] = useState<'people' | 'trace'>('people')
  const [users, setUsers] = useState<User[]>([])
  const [left, setLeft] = useState('priya')
  const [right, setRight] = useState('hannah')
  const [inbox, setInbox] = useState<Record<string, number>>({})
  const [seen, setSeen] = useState<Record<string, number>>({})
  const [requests, setRequests] = useState<CareRequest[]>([])
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [offline, setOffline] = useState(false)
  const [resetting, setResetting] = useState(false)

  const loadUsers = useCallback(() => api.users().then(setUsers), [])

  useEffect(() => {
    loadUsers().catch(() => setOffline(true))
  }, [loadUsers])

  // Poll the shared state the side panel and unread dots need.
  useEffect(() => {
    const tick = async () => {
      try {
        const [r, e, i] = await Promise.all([api.requests(), api.events(), api.inbox()])
        setRequests(r)
        setEvents(e)
        setInbox(i)
        setOffline(false)
      } catch {
        setOffline(true)
      }
    }
    tick()
    const t = setInterval(tick, 1500)
    return () => clearInterval(t)
  }, [])

  // Membership can change from chat (accepting an invite), so refresh people now and then.
  useEffect(() => {
    const t = setInterval(() => loadUsers().catch(() => undefined), 5000)
    return () => clearInterval(t)
  }, [loadUsers])

  // When a villager gets asked, jump the right-hand window to them (unless the left phone already shows them).
  useEffect(() => {
    const latest = requests[0]
    if (!latest) return
    const waiting = latest.asks.find((a) => a.status === 'asked' && a.villager_id !== left)
    const rightIsInvolved = latest.asks.some((a) => a.villager_id === right && a.status !== 'not_asked')
    if (waiting && !rightIsInvolved) setRight(waiting.villager_id)
  }, [requests, left, right])

  const onSeen = useCallback(
    (userId: string, lastId: number) => setSeen((s) => (s[userId] === lastId ? s : { ...s, [userId]: lastId })),
    [],
  )
  const unread: Record<string, boolean> = {}
  for (const [uid, last] of Object.entries(inbox)) unread[uid] = last > (seen[uid] ?? 0) && uid !== left && uid !== right

  async function reset() {
    if (!confirm('Reset the demo? All chats and requests will be cleared.')) return
    setResetting(true)
    try {
      await api.reset()
      setSeen({})
      setLeft('priya')
      setRight('hannah')
      await loadUsers()
    } catch (e) {
      alert((e as Error).message)
    } finally {
      setResetting(false)
    }
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <img src="/favicon.svg" alt="" width={34} height={34} />
          <div>
            <h1>Village</h1>
            <p>Short-notice kid care from SOM classmates you trust</p>
          </div>
        </div>
        <p className="topbar-hint">
          A browser stand-in for Slack. Each window is one person signed in to the demo workspace; Village is under
          <b> Apps</b>. Switch people with <b>Signed in as</b>, or from the people guide on the right.
        </p>
        <div className="controls">
            <button className="ghost" onClick={() => setShowPanel(!showPanel)}>
              {showPanel ? 'Hide' : 'Show'} side panel
            </button>
            <button className="ghost" onClick={reset} disabled={resetting}>
              {resetting ? 'Resetting…' : 'Reset demo'}
            </button>
        </div>
      </header>

      {offline && (
        <div className="banner">
          Can't reach the Village backend. Start it with <code>python3 backend/main.py</code> and this page will reconnect.
        </div>
      )}

      {users.length > 0 && (
        <main className={`demo ${showPanel ? 'with-panel' : ''}`}>
          <SlackWindow label="Window 1" users={users} userId={left} unread={unread} onChangeUser={setLeft} onSeen={onSeen}
            onUsersChanged={() => loadUsers().catch(() => undefined)} />
          <SlackWindow label="Window 2" users={users} userId={right} unread={unread} onChangeUser={setRight} onSeen={onSeen}
            onUsersChanged={() => loadUsers().catch(() => undefined)} />
          {showPanel && (
            <section className="panel">
              <div className="panel-tabs">
                <button className={tab === 'people' ? 'on' : ''} onClick={() => setTab('people')}>People</button>
                <button className={tab === 'trace' ? 'on' : ''} onClick={() => setTab('trace')}>Behind the scenes</button>
              </div>
              {tab === 'people' ? (
                <PeopleGuide users={users} left={left} right={right}
                  onOpen={(w, id) => (w === 1 ? setLeft(id) : setRight(id))} />
              ) : (
                <RequestPanel requests={requests} events={events} />
              )}
            </section>
          )}
        </main>
      )}

      <footer className="footer">
        Village is peer coordination between classmates, not a childcare service. Parents choose who's in their village and
        stay reachable the whole time.
      </footer>
    </div>
  )
}
