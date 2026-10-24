// Small display helpers.

/** "1:40pm" from a local ISO time like 2026-10-13T13:40 or a full timestamp. */
export function clock(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }).replace(' ', '').toLowerCase()
}

/** "Tue Oct 13" */
export function day(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })
}

export function span(start: string | null, end: string | null): string {
  return start && end ? `${day(start)}, ${clock(start)}–${clock(end)}` : '—'
}

export function tokens(n: number): string {
  return n >= 10_000 ? `${(n / 1000).toFixed(1)}k` : n.toLocaleString()
}

export function dollars(n: number): string {
  if (n === 0) return '$0'
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(3)}`
}

export const STATUS_LABEL: Record<string, string> = {
  draft: 'Waiting for parent to confirm',
  searching: 'Asking villagers',
  booked: 'Booked',
  no_match: 'No one free',
  cancelled: 'Cancelled',
  expired: 'Expired',
}

export const ASK_LABEL: Record<string, string> = {
  not_asked: 'Not asked',
  asked: 'Asked, waiting',
  accepted: 'Said yes',
  declined: 'Said no',
  countered: 'Offered other time',
  filled: 'Not needed',
  withdrawn: 'Withdrawn',
  withdrew: 'Backed out',
}
