// Shapes returned by the FastAPI backend (backend/models.py).

export type FormName = 'kid' | 'calendar' | 'availability'

export interface Button {
  label: string
  action: string
  request_id: number | null
  style: 'primary' | 'secondary' | 'danger'
  /** button: a plain tap. people: pick classmates. form: opens a form instead of acting right away. link: opens `url`. */
  kind: 'button' | 'people' | 'form' | 'link'
  form: FormName | null
  url: string | null
}

export interface Message {
  id: number
  user_id: string
  direction: 'in' | 'out'
  text: string
  buttons: Button[]
  buttons_used: boolean
  button_choice: string | null
  request_id: number | null
  created_at: string
}

export interface Kid {
  id: string
  name: string
  age: number
  notes: string
}

export interface Calendar {
  id: number
  label: string
  source: string
  kind: 'url' | 'file'
}

export interface VillageMember {
  villager_id: string
  name: string
  status: 'pending' | 'accepted' | 'declined'
  rank: number
}

export interface WeeklySlot {
  day: string
  start: string
  end: string
  kind: 'free' | 'busy'
}

export interface ParsedAvailability {
  slots: WeeklySlot[]
  caveats: string[]
  summary: string
}

export interface User {
  id: string
  name: string
  program: string
  color: string
  is_parent: boolean
  kids: Kid[]
  village: VillageMember[]
  in_villages_of: string[]
  calendars: Calendar[]
  availability_text: string
  availability_parsed: ParsedAvailability | null
  help_count_week: number
  help_count_total: number
  paused: boolean
  paused_until: string | null
  onboarding: string | null
  on_slack: boolean
}

export type PauseMode = 'today' | 'week' | 'indefinite' | 'resume'

export interface Ask {
  villager_id: string
  villager_name: string
  wave: number | null
  verdict: 'available' | 'maybe' | 'unavailable' | null
  verdict_reason: string | null
  decided_by: 'rules' | 'agent' | 'fallback' | null
  status: string
}

export interface CareRequest {
  id: number
  parent_id: string
  parent_name: string
  kid_name: string | null
  meeting_start: string | null
  meeting_minutes: number | null
  care_start: string | null
  care_end: string | null
  purpose: string | null
  location: string | null
  status: 'draft' | 'searching' | 'booked' | 'no_match' | 'cancelled' | 'expired'
  helper_name: string | null
  created_at: string
  asks: Ask[]
}

export interface TraceEvent {
  id: number
  request_id: number | null
  kind: 'step' | 'agent' | 'guard' | 'error'
  agent: string | null
  text: string
  data: Record<string, unknown> | null
  created_at: string
}

export interface UsageRow {
  agent: string
  runs: number
  input_tokens: number
  cached_tokens: number
  output_tokens: number
  cost_usd: number
}

export interface UsageSummary {
  by_agent: UsageRow[]
  total: UsageRow
  by_request: Record<string, UsageRow>
}
