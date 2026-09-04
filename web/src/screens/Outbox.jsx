import { useEffect, useState } from 'react'
import { api, causeLabel, rupees, when } from '../api'
import { Box, Empty, Panel, Spinner, Status, inputClass } from '../components/ui'

export default function Outbox({ tick, onOpenCase }) {
  const [messages, setMessages] = useState([])
  const [runs, setRuns] = useState([])
  const [runId, setRunId] = useState('live')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.runs().then((r) => setRuns(r.runs))
  }, [tick])

  useEffect(() => {
    setLoading(true)
    api
      .outbox({ run_id: runId, limit: 120 })
      .then((r) => setMessages(r.messages))
      .finally(() => setLoading(false))
  }, [tick, runId])

  return (
    <>
      <div className="mb-6 max-w-2xl">
        <h1 className="text-[19px] font-semibold tracking-[-0.02em]">Outbox</h1>
        <p className="mt-1 text-[13px] text-muted">
          Every outbound message writes a row here before it is sent, including the ones the gate
          stopped. Email is the only channel this project sends on; see the Audit log for whether a
          message actually left the building.
        </p>
      </div>

      <div className="mb-4 flex items-end gap-3">
        <label className="block">
          <span className="mb-1 block text-[12px] text-muted">Batch</span>
          <select className={inputClass} value={runId} onChange={(e) => setRunId(e.target.value)}>
            <option value="live">Live and injected</option>
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id}
              </option>
            ))}
          </select>
        </label>
        <span className="ml-auto text-[13px] text-muted">{messages.length} messages</span>
      </div>

      {loading ? (
        <Spinner />
      ) : messages.length === 0 ? (
        <Empty title="Nothing in the outbox for this batch">
          Advance the clock past a scheduled action, or run a policy comparison.
        </Empty>
      ) : (
        <div className="space-y-3">
          {messages.map((m) => (
            <Box key={m.id} className="px-5 py-4">
              <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[13px]">
                <span className="font-medium">{m.customer_name || 'Customer'}</span>
                <span className="text-muted">{causeLabel(m.root_cause)}</span>
                <span className="num text-muted">{rupees(m.amount_paise)}</span>
                <span className="text-muted">{m.channel}</span>
                <Status value={m.status} />
                <span className="num ml-auto text-muted">
                  {when(m.executed_at || m.scheduled_for)}
                </span>
              </div>

              {m.message_body ? (
                <p className="log max-w-3xl whitespace-pre-wrap">{m.message_body}</p>
              ) : (
                <p className="log text-blocked">
                  {m.blocked_reason || 'Scheduled, not yet sent'}
                </p>
              )}

              <div className="log mt-3 flex flex-wrap gap-x-5 gap-y-1 border-t border-rule pt-2 text-muted">
                <button className="hover:text-ink" onClick={() => onOpenCase(m.case_id)}>
                  case #{m.case_id}
                </button>
                <span>{m.order_id}</span>
                <span>{m.message_intent}</span>
                {m.message_source && <span>written by {m.message_source}</span>}
                {/* "sent" here means the outbox row was written, which is not
                    the same as a mail server accepting it. Without this, a
                    message refused by the allowlist reads as delivered. */}
                {m.status === 'sent' && (
                  <span
                    className={
                      { sent: 'text-recovered', failed: 'text-halt' }[m.delivery_status] ||
                      'text-atrisk'
                    }
                  >
                    {m.delivery_status === 'sent'
                      ? 'delivered'
                      : `not delivered — ${m.delivery_detail || 'rendered to the outbox only'}`}
                  </span>
                )}
                {m.suggests_alt_method && (
                  <span className="text-recovered">offers another payment method</span>
                )}
                {m.razorpay_link_id && <span>{m.razorpay_link_id}</span>}
                {m.resume_url && (
                  <a
                    href={m.resume_url}
                    target="_blank"
                    rel="noreferrer"
                    className="underline decoration-rule underline-offset-2 hover:text-ink"
                  >
                    open the page the customer sees
                  </a>
                )}
              </div>
            </Box>
          ))}
        </div>
      )}
    </>
  )
}
