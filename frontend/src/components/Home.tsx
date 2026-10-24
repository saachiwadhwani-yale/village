// The Village app's Home tab, laid out the same way as the Home tab in real Slack
// (backend/channels/slack.py): a section per topic, one line per item, a ⋯ menu on each line.

import { useState } from 'react'
import { api } from '../api'
import { clock, day } from '../format'
import type { FormName, Kid, PauseMode, User } from '../types'
import { AddPeopleForm, FormFor, KidForm } from './Forms'

interface Props {
  user: User
  users: User[]
  onChanged: () => void
}

export function Home({ user, users, onChanged }: Props) {
  const [form, setForm] = useState<FormName | 'people' | null>(null)
  const [editKid, setEditKid] = useState<Kid | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const firstName = (id: string) => (users.find((u) => u.id === id)?.name ?? id).split(' ')[0]
  const hasFreeTimes = user.calendars.length > 0 || !!user.availability_text

  async function run(fn: () => Promise<unknown>, done?: string) {
    try {
      await fn()
      setNote(done ?? null)
    } catch (e) {
      setNote((e as Error).message)
    }
    onChanged()
  }

  const villageIds = user.village.map((v) => v.villager_id)
  const status = (id: string, s: string) =>
    users.find((u) => u.id === id)?.paused && s === 'accepted'
      ? '⏸ Paused'
      : ({ accepted: '✅ Joined', pending: '⏳ Invited', declined: '✖️ Said not right now' } as Record<string, string>)[s]
  const pausedText =
    user.paused_until === 'indefinite'
      ? '⏸ Paused until you turn it back on'
      : `⏸ Paused until ${day(user.paused_until)} at ${clock(user.paused_until)}`

  // calendar_joined: a parent who finished setup and was later asked for their free times.
  const setupDone = user.onboarding === 'done' || user.onboarding === 'calendar_joined'

  return (
    <div className="home">
      <h2>🏡 Your Village profile</h2>
      <p className="home-sub">To ask for help, just message Village in the <b>Messages</b> tab.</p>
      {note && <p className="home-note">{note}</p>}
      {!setupDone ? (
        <Line text={<><b>👋 Finish setting up in the Messages tab.</b> Village is asking you a few quick questions there.</>}
          action={<button className="sbtn small" onClick={() => run(() => api.restartSetup(user.id), 'Setup started again in Messages.')}>Start over</button>} />
      ) : !hasFreeTimes && (!user.is_parent || user.in_villages_of.length > 0) && (
        <Line text={<><b>⚠️ Connect a calendar or describe when you're usually free,</b> so Village knows when to ask you.</>}
          action={<button className="sbtn small" onClick={() => setForm('calendar')}>Connect a calendar</button>} />
      )}

      {user.is_parent && (
        <>
          <Section title="Kids" action={<button className="sbtn small" onClick={() => setForm('kid')}>Add a kid</button>}>
            {user.kids.map((k) => (
              <Line key={k.id} text={<>{k.name}, {k.age}{(k.notes || k.allergies) && <span className="muted">  ·  🔒 allergies and notes saved</span>}</>}
                menu={[[`Edit ${k.name}`, () => setEditKid(k)], [`Remove ${k.name}`, () => confirm(`Remove ${k.name}? Their notes will be deleted.`) && run(() => api.removeKid(user.id, k.id))]]} />
            ))}
          </Section>

          <Section title="Your village" action={<button className="sbtn small" onClick={() => setForm('people')}>Add people</button>}
            hint="I only ask people who look free, and spread asks around so no one is asked too often.">
            {user.village.length === 0 && <p className="muted">No one yet. Add classmates you'd trust with your kid.</p>}
            {user.village.map((m) => (
              <Line key={m.villager_id} text={<>{m.name}  <span className="muted">·</span>  {status(m.villager_id, m.status)}</>}
                menu={[['Remove from village', () => run(() => api.setVillage(user.id, villageIds.filter((x) => x !== m.villager_id)))]]} />
            ))}
          </Section>
        </>
      )}

      <Section title="Calendars" action={<button className="sbtn small" onClick={() => setForm('calendar')}>Connect a calendar</button>}
        hint="I only see busy times, never event names.">
        {user.calendars.length === 0 && <p className="muted">None connected.</p>}
        {user.calendars.map((c) => (
          <Line key={c.id} text={c.label} menu={[['Disconnect', () => run(() => api.removeCalendar(user.id, c.id))]]} />
        ))}
      </Section>

      <Section title="Availability in your words"
        action={<button className="sbtn small" onClick={() => setForm('availability')}>{user.availability_text ? 'Edit' : 'Write'}</button>}>
        <p className={user.availability_text ? '' : 'muted'}>
          {user.availability_text || 'Nothing written. Optional, e.g. "free Tue/Thu after 1".'}
        </p>
      </Section>

      <Section title={user.is_parent ? 'Helping other parents' : 'Helping parents'}
        action={
          <select className="pause-menu" value="" onChange={(e) => e.target.value && run(() => api.pause(user.id, e.target.value as PauseMode))}>
            <option value="">Change…</option>
            {user.paused && <option value="resume">Start taking requests</option>}
            <option value="today">Pause for today</option>
            <option value="week">Pause for a week</option>
            <option value="indefinite">Pause until I turn it back on</option>
          </select>
        }>
        <p>{user.paused ? pausedText : '🟢 Taking requests'}  <span className="muted">·  helped {user.help_count_week} this week, {user.help_count_total} all-time</span></p>
        {user.in_villages_of.map((pid) => (
          <Line key={pid} text={<>In <b>{users.find((u) => u.id === pid)?.name}</b>'s village</>}
            action={<button className="sbtn small danger"
              onClick={() => confirm(`Leave ${firstName(pid)}'s village? You won't get their requests anymore, and ${firstName(pid)} will be told you've stepped back. A time you already said yes to stays booked.`)
                && run(() => api.leaveVillage(user.id, pid))}>Leave</button>} />
        ))}
      </Section>

      {!user.is_parent && (
        <Section title="Are you a parent?" action={<button className="sbtn small" onClick={() => setForm('kid')}>Add your kid</button>}>
          <p className="muted">Add your kid to start your own village.</p>
        </Section>
      )}

      {setupDone && (
        <div className="home-footer">
          <button className="sbtn" onClick={() => run(() => api.restartSetup(user.id), 'Setup started again in Messages.')}>
            Redo setup questions
          </button>
        </div>
      )}

      {editKid && (
        <KidForm user={user} kid={editKid} onClose={() => setEditKid(null)} onDone={() => { setEditKid(null); onChanged() }} />
      )}
      {form === 'people' && (
        <AddPeopleForm user={user} users={users} onClose={() => setForm(null)} onDone={() => { setForm(null); onChanged() }} />
      )}
      {form && form !== 'people' && (
        <FormFor form={form} user={user} onClose={() => setForm(null)} onDone={() => { setForm(null); onChanged() }} />
      )}
    </div>
  )
}

function Section({ title, hint, action, children }: { title: string; hint?: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="home-section">
      <Line text={<b>{title}</b>} action={action} />
      {hint && <p className="home-hint">{hint}</p>}
      {children}
    </section>
  )
}

/** One line with an optional button on the right, or a ⋯ menu like Slack's overflow menu. */
function Line({ text, action, menu }: { text: React.ReactNode; action?: React.ReactNode; menu?: [string, () => unknown][] }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="home-line">
      <span className="home-line-text">{text}</span>
      {action}
      {menu && (
        <span className="overflow">
          <button className="overflow-btn" onClick={() => setOpen(!open)} aria-label="More">⋯</button>
          {open && (
            <span className="overflow-menu" onMouseLeave={() => setOpen(false)}>
              {menu.map(([label, fn]) => (
                <button key={label} onClick={() => { setOpen(false); fn() }}>{label}</button>
              ))}
            </span>
          )}
        </span>
      )}
    </div>
  )
}
