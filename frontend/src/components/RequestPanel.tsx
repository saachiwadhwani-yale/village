import { useState } from 'react'
import { ASK_LABEL, STATUS_LABEL, clock, dollars, span, tokens } from '../format'
import type { CareRequest, TraceEvent, UsageRow, UsageSummary } from '../types'

interface Props {
  requests: CareRequest[]
  events: TraceEvent[]
  usage: UsageSummary | null
}

const AGENT_NAMES: Record<string, string> = {
  intake: 'Intake agent',
  availability: 'Availability agent',
  reply: 'Reply agent',
  profile: 'Profile agent',
}

export function RequestPanel({ requests, events, usage }: Props) {
  const [picked, setPicked] = useState<number | null>(null)
  const req = requests.find((r) => r.id === picked) ?? requests[0]
  const trace = req ? events.filter((e) => e.request_id === req.id) : []
  const cost = req && usage ? usage.by_request[String(req.id)] : undefined

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Behind the scenes</h2>
        {requests.length > 1 && (
          <select value={req?.id} onChange={(e) => setPicked(Number(e.target.value))}>
            {requests.map((r) => (
              <option key={r.id} value={r.id}>
                #{r.id} {r.parent_name.split(' ')[0]} · {r.purpose ?? 'request'}
              </option>
            ))}
          </select>
        )}
      </div>

      {!req ? (
        <div className="panel-empty">
          <p>When a parent sends a request, you'll see each step here:</p>
          <ol>
            <li>The <b>intake agent</b> reads the message and confirms the time.</li>
            <li>Every villager's connected calendars are <b>screened by rules</b>. Unclear cases go to that villager's own <b>availability agent</b>.</li>
            <li>The best few are asked. The <b>reply agent</b> reads free-text answers.</li>
            <li>The first "yes" books it. Everyone else is told.</li>
          </ol>
        </div>
      ) : (
        <>
          <div className="req-card">
            <div className="req-title">
              <span>
                {req.parent_name.split(' ')[0]} → {req.kid_name ?? 'kid'}
                {req.purpose ? ` · ${req.purpose}` : ''}
              </span>
              <span className={`status-pill ${req.status}`}>{STATUS_LABEL[req.status]}</span>
            </div>
            <div className="req-meta">
              Care window <b>{span(req.care_start, req.care_end)}</b>
              {req.location && <> · {req.location}</>}
              {req.helper_name && (
                <>
                  {' '}
                  · Helper <b>{req.helper_name}</b>
                </>
              )}
            </div>
          </div>

          {req.asks.length > 0 && (
            <table className="asks">
              <thead>
                <tr>
                  <th>Villager</th>
                  <th>Looks</th>
                  <th>Decided by</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {req.asks.map((a) => (
                  <tr key={a.villager_id} title={a.verdict_reason ?? ''}>
                    <td>
                      <div className="ask-name">{a.villager_name}</div>
                      <div className="ask-reason">{a.verdict_reason}</div>
                    </td>
                    <td>
                      <span className={`verdict ${a.verdict}`}>{a.verdict}</span>
                    </td>
                    <td>
                      <span className={`by ${a.decided_by}`}>{a.decided_by === 'agent' ? '✦ agent' : a.decided_by === 'rules' ? '⚙ rules' : a.decided_by}</span>
                    </td>
                    <td className={`ask-status ${a.status}`}>
                      {ASK_LABEL[a.status] ?? a.status}
                      {a.wave ? <span className="wave">wave {a.wave}</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h3>Trace</h3>
          <ol className="trace">
            {trace.map((e) => (
              <li key={e.id} className={`kind-${e.kind}`}>
                <span className="trace-time">{clock(e.created_at)}</span>
                {e.agent && <span className="trace-agent">{AGENT_NAMES[e.agent] ?? e.agent}</span>}
                <span className="trace-text">{e.text}</span>
              </li>
            ))}
          </ol>

          {cost && (
            <>
              <h3>This request's cost</h3>
              <CostRow row={cost} />
            </>
          )}
        </>
      )}

      {usage && usage.total.runs > 0 && (
        <>
          <h3>All agent runs so far</h3>
          <table className="usage">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Runs</th>
                <th>Input</th>
                <th>Cached</th>
                <th>Output</th>
                <th>Cost</th>
              </tr>
            </thead>
            <tbody>
              {[...usage.by_agent, usage.total].map((r) => (
                <tr key={r.agent} className={r.agent === 'total' ? 'total' : ''}>
                  <td>{r.agent === 'total' ? 'Total' : (AGENT_NAMES[r.agent] ?? r.agent)}</td>
                  <td>{r.runs}</td>
                  <td>{tokens(r.input_tokens)}</td>
                  <td>{tokens(r.cached_tokens)}</td>
                  <td>{tokens(r.output_tokens)}</td>
                  <td>{dollars(r.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}

function CostRow({ row }: { row: UsageRow }) {
  return (
    <div className="cost-row">
      <div>
        <b>{row.runs}</b> agent runs
      </div>
      <div>
        <b>{tokens(row.input_tokens)}</b> in ({tokens(row.cached_tokens)} cached)
      </div>
      <div>
        <b>{tokens(row.output_tokens)}</b> out
      </div>
      <div>
        <b>{dollars(row.cost_usd)}</b>
      </div>
    </div>
  )
}
