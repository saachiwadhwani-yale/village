import type { ReactNode } from 'react'

/** Render Slack-style text: *bold*, _italic_, and line breaks. Everything else is plain text. */
export function Mrkdwn({ text }: { text: string }) {
  const parts: ReactNode[] = []
  const pattern = /\*([^*\n]+)\*|_([^_\n]+)_/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = pattern.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    parts.push(m[1] !== undefined ? <b key={m.index}>{m[1]}</b> : <i key={m.index}>{m[2]}</i>)
    last = m.index + m[0].length
  }
  if (last < text.length) parts.push(text.slice(last))
  return <span className="mrkdwn">{parts}</span>
}
