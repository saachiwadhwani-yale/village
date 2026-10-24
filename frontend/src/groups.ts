import type { User } from './types'

export type Group = 'parents' | 'classmates' | 'new'

/** Who's who in the demo: set-up parents, set-up classmates, and people who haven't done setup yet. */
export const GROUPS: { key: Group; title: string; about: string }[] = [
  { key: 'parents', title: 'Parents', about: 'Set up with a kid and a village.' },
  { key: 'classmates', title: 'Classmates', about: 'Set up with a calendar or written availability.' },
  { key: 'new', title: 'New to Village', about: "Not set up yet. They're up to you to set up if you want." },
]

export function groupOf(u: User): Group {
  const setUp = u.onboarding === 'done' || u.onboarding === 'calendar_joined'
  if (!setUp) return 'new'
  return u.is_parent ? 'parents' : 'classmates'
}

const firstName = (users: User[], id: string) => users.find((u) => u.id === id)?.name.split(' ')[0] ?? id

function and(names: string[]): string {
  return names.length <= 1 ? (names[0] ?? '') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/** How someone is set up right now, e.g. "Arjun, 4. Villagers: Sam, Mei, Hannah (invited)." */
export function describe(u: User, users: User[]): string[] {
  const slack = u.on_slack ? ' In Slack, real user.' : ''
  if (groupOf(u) === 'new') return ['Not set up yet.' + slack]
  const lines: string[] = []
  if (u.is_parent) {
    const kids = u.kids.map((k) => `${k.name}, ${k.age}`).join('; ')
    const villagers = u.village
      .filter((v) => v.status !== 'declined')
      .map((v) => v.name.split(' ')[0] + (v.status === 'pending' ? ' (invited)' : ''))
    lines.push(`${kids}. ${villagers.length ? `Villagers: ${villagers.join(', ')}.` : 'No villagers yet.'}`)
  }
  const ins = u.in_villages_of.map((id) => `${firstName(users, id)}'s`)
  const invitedBy = u.invited_to.map((id) => `${firstName(users, id)}'s`)
  if (ins.length) lines.push(`In ${and(ins)} village${ins.length > 1 ? 's' : ''}.`)
  if (invitedBy.length) lines.push(`Invited to ${and(invitedBy)} village${invitedBy.length > 1 ? 's' : ''}.`)
  if (!u.is_parent && !ins.length && !invitedBy.length) lines.push('Not in a village yet.')
  const demo = u.calendars.filter((c) => c.kind === 'file').length
  const real = u.calendars.length - demo
  const setup = [
    demo ? `Connected to ${demo > 1 ? 'demo calendars' : 'demo calendar'}.` : '',
    real ? `Connected to ${real > 1 ? 'calendars' : 'a calendar'} by link.` : '',
    u.availability_text ? 'Availability described.' : '',
  ].filter(Boolean)
  if (setup.length && (!u.is_parent || ins.length || invitedBy.length)) lines.push(setup.join(' '))
  return [lines.join(' ') + slack]
}
