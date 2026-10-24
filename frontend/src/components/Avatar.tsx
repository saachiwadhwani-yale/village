import type { User } from '../types'

export function Avatar({ user, size = 28 }: { user: Pick<User, 'name' | 'color'>; size?: number }) {
  const initials = user.name
    .split(' ')
    .map((p) => p[0])
    .slice(0, 2)
    .join('')
  return (
    <span className="avatar" style={{ background: user.color, width: size, height: size, fontSize: size * 0.4 }}>
      {initials}
    </span>
  )
}
