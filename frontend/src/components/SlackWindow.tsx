// A Slack-like window for one person, signed in to a demo "SOM Village" workspace: channels and
// direct messages in the sidebar (scenery), and the Village app with its Messages and Home tabs.
// Who you're signed in as is a demo control above the window, outside the fake Slack.

import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { clock } from '../format'
import { GROUPS, groupOf } from '../groups'
import { Mrkdwn } from '../mrkdwn'
import type { Button, FormName, Message, User } from '../types'
import { Avatar } from './Avatar'
import { FormFor } from './Forms'
import { Home } from './Home'

const CHANNELS = ['general', 'mba-2027', 'som-parents', 'recruiting']

type View = { kind: 'app' } | { kind: 'channel'; name: string } | { kind: 'dm'; userId: string }

interface Props {
  label: string
  users: User[]
  userId: string
  unread: Record<string, boolean>
  onChangeUser: (id: string) => void
  onSeen: (userId: string, lastId: number) => void
  onUsersChanged: () => void
}

export function SlackWindow({ label, users, userId, unread, onChangeUser, onSeen, onUsersChanged }: Props) {
  const [view, setView] = useState<View>({ kind: 'app' })
  const [tab, setTab] = useState<'messages' | 'home'>('messages')
  const [messages, setMessages] = useState<Message[]>([])
  const [typing, setTyping] = useState(false)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState<FormName | null>(null)
  const [appUnread, setAppUnread] = useState(0)
  const lastSeen = useRef(0)
  const scroller = useRef<HTMLDivElement>(null)
  const user = users.find((u) => u.id === userId)
  const onApp = view.kind === 'app'

  // Opening Village for the first time starts setup, like a Slack user opening the app.
  useEffect(() => {
    if (user && user.onboarding === null) api.open(userId).then(onUsersChanged).catch(() => undefined)
  }, [userId, user?.onboarding]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setView({ kind: 'app' })
    setTab('messages')
  }, [userId])

  useEffect(() => {
    let alive = true
    setMessages([])
    lastSeen.current = 0
    setAppUnread(0)
    const load = async () => {
      try {
        const res = await api.messages(userId)
        if (!alive) return
        setMessages(res.messages)
        setTyping(res.typing)
        const last = res.messages.at(-1)
        if (last) onSeen(userId, last.id)
      } catch {
        /* backend restarting; try again next tick */
      }
    }
    load()
    const timer = setInterval(load, 1000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [userId, onSeen])

  // Unread badge on the Village app while you're looking at something else.
  useEffect(() => {
    const newest = messages.filter((m) => m.direction === 'out').at(-1)?.id ?? 0
    if (onApp && tab === 'messages') {
      lastSeen.current = newest
      setAppUnread(0)
    } else {
      setAppUnread(messages.filter((m) => m.direction === 'out' && m.id > lastSeen.current).length)
    }
  }, [messages, onApp, tab])

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' })
  }, [messages.length, typing, tab, view])

  async function send(text: string) {
    const clean = text.trim()
    if (!clean) return
    setDraft('')
    setError(null)
    setTyping(true)
    try {
      await api.send(userId, clean)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  async function press(m: Message, b: Button, values: string[] = []) {
    setMessages((ms) => ms.map((x) => (x.id === m.id ? { ...x, buttons_used: true } : x)))
    setTyping(true)
    try {
      await api.press(userId, b.action, b.request_id, m.id, values)
      onUsersChanged()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  async function joinAsNew() {
    const name = prompt('Name for the new classmate (e.g. "Jamie Rivera"):')
    if (!name?.trim()) return
    const u = await api.newUser(name.trim(), 'SOM')
    onUsersChanged()
    onChangeUser(u.id)
  }

  const others = users.filter((u) => u.id !== userId)
  const dmUser = view.kind === 'dm' ? users.find((u) => u.id === view.userId) : undefined

  return (
    <div className="slack-frame">
      <div className="demo-bar" title="Demo control: in the real app, each person has their own Slack account.">
        <span className="demo-bar-label">{label}</span>
        <label>
          Signed in as
          <select value={userId} onChange={(e) => onChangeUser(e.target.value)}>
            {GROUPS.map((g) => (
              <optgroup key={g.key} label={g.title}>
                {users.filter((u) => groupOf(u) === g.key).map((u) => (
                  <option key={u.id} value={u.id}>{unread[u.id] ? '● ' : ''}{u.name}</option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <button className="demo-bar-new" onClick={joinAsNew}>+ New classmate</button>
      </div>

      <section className="slack">
        <aside className="slack-side">
          <div className="slack-workspace">SOM Village ▾</div>
          <div className="slack-me">
            {user && <Avatar user={user} size={20} />}
            <span>{user?.name ?? '…'}</span>
            <span className="presence on" />
          </div>

          <div className="slack-section">Channels</div>
          {CHANNELS.map((c) => (
            <button key={c} className={`slack-item ${view.kind === 'channel' && view.name === c ? 'active' : ''}`}
              onClick={() => setView({ kind: 'channel', name: c })}>
              <span className="hash">#</span> {c}
            </button>
          ))}

          <div className="slack-section">Direct messages</div>
          {others.slice(0, 8).map((u) => (
            <button key={u.id} className={`slack-item ${view.kind === 'dm' && view.userId === u.id ? 'active' : ''}`}
              onClick={() => setView({ kind: 'dm', userId: u.id })}>
              <span className="dm-avatar"><Avatar user={u} size={18} /><span className="presence" /></span>
              <span className="slack-name">{u.name}</span>
            </button>
          ))}

          <div className="slack-section">Apps</div>
          <button className={`slack-item ${onApp ? 'active' : ''} ${appUnread ? 'unread' : ''}`}
            onClick={() => setView({ kind: 'app' })}>
            <span className="app-icon">🏡</span>
            <span className="slack-name">Village</span>
            {appUnread > 0 && <span className="badge">{appUnread}</span>}
          </button>
        </aside>

        <div className="slack-main">
          {view.kind === 'channel' && (
            <Scenery title={`# ${view.name}`}
              text={`This is #${view.name}, part of the demo workspace's scenery. Village lives under Apps in the sidebar.`} />
          )}
          {view.kind === 'dm' && dmUser && (
            <Scenery title={dmUser.name} avatar={dmUser}
              text={`Direct messages between classmates aren't part of the demo. When Village says to message ${dmUser.name.split(' ')[0]} directly, this is where you'd do it in real Slack.`} />
          )}

          {onApp && (
            <>
              <header className="slack-header">
                <div className="slack-title">
                  <span className="app-icon big">🏡</span>
                  <b>Village</b>
                  <span className="app-badge">APP</span>
                </div>
                <nav className="slack-tabs">
                  <button className={tab === 'messages' ? 'active' : ''} onClick={() => setTab('messages')}>Messages</button>
                  <button className={tab === 'home' ? 'active' : ''} onClick={() => setTab('home')}>Home</button>
                </nav>
              </header>

              {tab === 'home' && user ? (
                <div className="slack-scroll">
                  <Home user={user} users={users} onChanged={onUsersChanged} />
                </div>
              ) : (
                <>
                  <div className="slack-scroll" ref={scroller}>
                    {messages.length === 0 && (
                      <div className="slack-empty">
                        <span className="app-icon huge">🏡</span>
                        <p>This is the very beginning of your messages with <b>Village</b>.</p>
                      </div>
                    )}
                    {messages.map((m, i) => {
                      const prev = messages[i - 1]
                      const grouped = prev && prev.direction === m.direction
                      return (
                        <div key={m.id} className={`smsg ${grouped ? 'grouped' : ''}`}>
                          <div className="smsg-gutter">
                            {!grouped && (m.direction === 'out'
                              ? <span className="app-icon avatar">🏡</span>
                              : user && <Avatar user={user} size={36} />)}
                            {grouped && <span className="smsg-hover-time">{clock(m.created_at)}</span>}
                          </div>
                          <div className="smsg-body">
                            {!grouped && (
                              <div className="smsg-head">
                                <b>{m.direction === 'out' ? 'Village' : user?.name}</b>
                                {m.direction === 'out' && <span className="app-badge">APP</span>}
                                <span className="smsg-time">{clock(m.created_at)}</span>
                              </div>
                            )}
                            <div className="smsg-text"><Mrkdwn text={m.text} /></div>
                            <Controls m={m} users={users} me={userId} onPress={press} onForm={setForm} />
                          </div>
                        </div>
                      )
                    })}
                    {typing && <div className="slack-typing">Village is typing…</div>}
                  </div>

                  {error && <div className="slack-error">{error}</div>}
                  <div className="slack-composer">
                    <form onSubmit={(e) => { e.preventDefault(); send(draft) }}>
                      <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Message Village" maxLength={1000} />
                      <button type="submit" disabled={!draft.trim()} aria-label="Send">➤</button>
                    </form>
                  </div>
                </>
              )}
            </>
          )}
        </div>

        {form && user && (
          <FormFor form={form} user={user} onClose={() => setForm(null)} onDone={() => { setForm(null); onUsersChanged() }} />
        )}
      </section>
    </div>
  )
}

function Scenery({ title, text, avatar }: { title: string; text: string; avatar?: User }) {
  return (
    <>
      <header className="slack-header">
        <div className="slack-title">
          {avatar && <Avatar user={avatar} size={24} />}
          <b>{title}</b>
        </div>
        <nav className="slack-tabs"><button className="active">Messages</button></nav>
      </header>
      <div className="slack-scroll">
        <div className="slack-empty"><p>{text}</p></div>
      </div>
      <div className="slack-composer">
        <form onSubmit={(e) => e.preventDefault()}>
          <input disabled placeholder={`Message ${title}`} />
          <button type="submit" disabled aria-label="Send">➤</button>
        </form>
      </div>
    </>
  )
}

/** A message's buttons, people picker, or form buttons, drawn like Slack's Block Kit. */
function Controls({ m, users, me, onPress, onForm }: {
  m: Message
  users: User[]
  me: string
  onPress: (m: Message, b: Button, values?: string[]) => void
  onForm: (f: FormName) => void
}) {
  if (m.buttons.length === 0) return null
  if (m.buttons_used) {
    return m.button_choice ? <div className="smsg-choice">✓ {m.button_choice}</div> : null
  }
  const plain = m.buttons.filter((b) => b.kind !== 'people')
  const picker = m.buttons.find((b) => b.kind === 'people')
  return (
    <div className="smsg-controls">
      {picker && <PeoplePicker users={users.filter((u) => u.id !== me)} label={picker.label} onDone={(ids) => onPress(m, picker, ids)} />}
      {plain.length > 0 && (
        <div className="smsg-buttons">
          {plain.map((b) =>
            b.kind === 'link' && b.url ? (
              <a key={b.action} className={`sbtn ${b.style}`} href={b.url} target="_blank" rel="noreferrer">
                {b.label}
              </a>
            ) : (
              <button key={b.action + b.label} className={`sbtn ${b.style}`}
                onClick={() => (b.kind === 'form' && b.form ? onForm(b.form) : onPress(m, b))}>
                {b.label}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  )
}

function PeoplePicker({ users, label, onDone }: { users: User[]; label: string; onDone: (ids: string[]) => void }) {
  const [open, setOpen] = useState(false)
  const [picked, setPicked] = useState<string[]>([])
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]))
  return (
    <div className="picker">
      <button className="picker-field" onClick={() => setOpen(!open)}>
        {picked.length === 0 ? <span className="muted">{label}</span>
          : picked.map((id) => <span key={id} className="chip">{users.find((u) => u.id === id)?.name}</span>)}
        <span className="caret">▾</span>
      </button>
      {picked.length > 0 && (
        <button className="sbtn primary" onClick={() => { setOpen(false); onDone(picked) }}>
          Invite {picked.length}
        </button>
      )}
      {open && (
        <div className="picker-menu">
          {users.map((u) => (
            <label key={u.id}>
              <input type="checkbox" checked={picked.includes(u.id)} onChange={() => toggle(u.id)} />
              <Avatar user={u} size={18} /> {u.name}
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
