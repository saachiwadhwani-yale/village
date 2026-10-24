// Slack-style modals: add a kid, connect a calendar, describe availability.
// The same forms open from setup questions in Messages and from the Home tab.

import { useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api'
import type { FormName, User } from '../types'

interface FormProps {
  user: User
  onDone: (u?: User) => void
  onClose: () => void
}

function Modal({ title, submit, busy, error, onSubmit, onClose, children }: {
  title: string
  submit: string
  busy: boolean
  error: string | null
  onSubmit: () => void
  onClose: () => void
  children: ReactNode
}) {
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <form
        className="modal"
        onMouseDown={(e) => e.stopPropagation()}
        onSubmit={(e) => {
          e.preventDefault()
          onSubmit()
        }}
      >
        <header>
          <h3>{title}</h3>
          <button type="button" className="modal-x" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {error && <p className="modal-error">{error}</p>}
        <footer>
          <button type="button" className="sbtn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="sbtn primary" disabled={busy}>
            {busy ? 'Working…' : submit}
          </button>
        </footer>
      </form>
    </div>
  )
}

function useSubmit(onDone: (u?: User) => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async (fn: () => Promise<User>) => {
    setBusy(true)
    setError(null)
    try {
      onDone(await fn())
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, run }
}

export function KidForm({ user, onDone, onClose }: FormProps) {
  const [name, setName] = useState('')
  const [age, setAge] = useState('')
  const [notes, setNotes] = useState('')
  const { busy, error, run } = useSubmit(onDone)
  return (
    <Modal title="Add a kid" submit="Save" busy={busy} error={error} onClose={onClose}
      onSubmit={() => run(() => api.addKid(user.id, name.trim(), Number(age), notes))}>
      <label>First name<input value={name} onChange={(e) => setName(e.target.value)} maxLength={40} required /></label>
      <label>Age<input value={age} onChange={(e) => setAge(e.target.value)} inputMode="numeric" pattern="\d{1,2}" required /></label>
      <label>
        Notes for whoever helps <span className="optional">(optional)</span>
        <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} maxLength={500} />
        <small>Allergies, favorite things, what's in the bag. Only shared with the classmate who says yes, after they say yes.</small>
      </label>
    </Modal>
  )
}

export function CalendarForm({ user, onDone, onClose }: FormProps) {
  const [label, setLabel] = useState('')
  const [url, setUrl] = useState('')
  const { busy, error, run } = useSubmit(onDone)
  return (
    <Modal title="Connect a calendar" submit="Connect" busy={busy} error={error} onClose={onClose}
      onSubmit={() => run(() => api.addCalendar(user.id, label.trim() || 'Calendar', url.trim()))}>
      <label>Label <span className="optional">(optional)</span>
        <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Yale Outlook" maxLength={40} />
      </label>
      <label>Private iCal link
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://… or webcal://…" required />
      </label>
      <div className="modal-help">
        <p><b>Google:</b> Settings → your calendar → Integrate calendar → <i>Secret address in iCal format</i></p>
        <p><b>Outlook / Yale:</b> Settings → Calendar → Shared calendars → Publish a calendar → <i>Can view when I'm busy</i> → copy the ICS link</p>
        <p><b>Apple:</b> right-click the calendar → Share → Public calendar → copy the link</p>
        <p>To try it without your own calendar, use Google's public US holidays calendar:<br />
          <code>https://calendar.google.com/calendar/ical/en.usa%23holiday%40group.v.calendar.google.com/public/basic.ics</code></p>
      </div>
    </Modal>
  )
}

export function AvailabilityForm({ user, onDone, onClose }: FormProps) {
  const [text, setText] = useState(user.availability_text)
  const { busy, error, run } = useSubmit(onDone)
  return (
    <Modal title="Your availability" submit="Save" busy={busy} error={error} onClose={onClose}
      onSubmit={() => run(() => api.setAvailability(user.id, text))}>
      <label>When are you usually free?
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={4} maxLength={1500}
          placeholder="e.g. Usually free Tue/Thu afternoons after 1. Never Fridays." />
        <small>Village turns this into weekly time slots once, when you save.</small>
      </label>
    </Modal>
  )
}

/** Slack's "Add to your village" modal: pick classmates; each gets an invite first. */
export function AddPeopleForm({ user, users, onDone, onClose }: FormProps & { users: User[] }) {
  const inVillage = new Set(user.village.map((v) => v.villager_id))
  const choices = users.filter((u) => u.id !== user.id && !inVillage.has(u.id))
  const [picked, setPicked] = useState<string[]>([])
  const { busy, error, run } = useSubmit(onDone)
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]))
  return (
    <Modal title="Add to your village" submit="Invite" busy={busy} error={error} onClose={onClose}
      onSubmit={() => run(() => api.setVillage(user.id, [...user.village.map((v) => v.villager_id), ...picked]))}>
      <div className="modal-field">
        <b>Who would you trust with your kid?</b>
        <div className="people-list">
          {choices.length === 0 && <span className="muted">Everyone is already in your village.</span>}
          {choices.map((u) => (
            <label key={u.id}>
              <input type="checkbox" checked={picked.includes(u.id)} onChange={() => toggle(u.id)} />
              {u.name}
            </label>
          ))}
        </div>
        <small>Each person gets an invite first and can say yes or no.</small>
      </div>
    </Modal>
  )
}

export function FormFor({ form, ...props }: FormProps & { form: FormName }) {
  if (form === 'kid') return <KidForm {...props} />
  if (form === 'calendar') return <CalendarForm {...props} />
  return <AvailabilityForm {...props} />
}
