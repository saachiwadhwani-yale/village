import { GROUPS, describe, groupOf } from '../groups'
import type { User } from '../types'
import { Avatar } from './Avatar'

interface Props {
  users: User[]
  left: string
  right: string
  onOpen: (window: 1 | 2, userId: string) => void
}

/** The demo's cast, grouped by how far along setup they are, with how each is set up right now. */
export function PeopleGuide({ users, left, right, onOpen }: Props) {
  return (
    <div className="guide">
      <p className="guide-intro">
        Everyone here is made up. Open anyone in either window. People move between groups as they finish setup.
      </p>
      {GROUPS.map((g) => {
        const people = users.filter((u) => groupOf(u) === g.key && !u.on_slack)   // real Slack users aren't played here
        return (
          <section key={g.key} className="guide-group">
            <h3>{g.title}</h3>
            <p className="guide-use">{g.about}</p>
            {people.length === 0 && <p className="muted">No one right now.</p>}
            {people.map((u) => (
              <div key={u.id} className="guide-person">
                <Avatar user={u} size={28} />
                <div className="guide-text">
                  <b>{u.name}</b>
                  {describe(u, users).map((line) => <span key={line}>{line}</span>)}
                </div>
                <div className="guide-open">
                  <button className={left === u.id ? 'on' : ''} onClick={() => onOpen(1, u.id)}>Window 1</button>
                  <button className={right === u.id ? 'on' : ''} onClick={() => onOpen(2, u.id)}>Window 2</button>
                </div>
              </div>
            ))}
          </section>
        )
      })}
    </div>
  )
}
