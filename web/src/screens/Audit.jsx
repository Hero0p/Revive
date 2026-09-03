import { useEffect, useState } from 'react'
import { api, rupees, when } from '../api'
import { Box, Button, Empty, Spinner } from '../components/ui'

const KIND = {
  webhook: { label: 'webhook', tone: 'text-recovered' },
  webhook_rejected: { label: 'webhook', tone: 'text-halt' },
  case: { label: 'case', tone: 'text-ink' },
  decision: { label: 'decision', tone: 'text-ink' },
  scheduled: { label: 'scheduled', tone: 'text-atrisk' },
  message: { label: 'message', tone: 'text-ink' },
  blocked: { label: 'blocked', tone: 'text-blocked' },
  recovered: { label: 'paid', tone: 'text-recovered' },
}

const FILTERS = [
  ['all', 'Everything'],
  ['webhook', 'Webhooks'],
  ['decision', 'Decisions'],
  ['message', 'Messages'],
  ['blocked', 'Stopped'],
]

export default function Audit({ tick, onOpenCase }) {
  const [data, setData] = useState(null)
  const [filter, setFilter] = useState('all')
  const [liveOnly, setLiveOnly] = useState(true)

  useEffect(() => {
    api.audit({ live_only: liveOnly, limit: 400 }).then(setData)
  }, [tick, liveOnly])

  if (!data) return <Spinner />

  const entries = data.entries.filter((e) => {
    if (filter === 'all') return true
    if (filter === 'webhook') return e.kind.startsWith('webhook')
    return e.kind === filter
  })

  return (
    <>
      <div className="mb-6 max-w-2xl">
        <h1 className="text-[19px] font-semibold tracking-[-0.02em]">Audit log</h1>
        <p className="mt-1 text-[13px] text-muted">
          Every webhook, classification, decision, and message in the order it happened. This is
          the whole record — nothing about a payment happens outside it.
        </p>
      </div>

      <Delivery status={data.delivery} />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        {FILTERS.map(([id, label]) => (
          <Button key={id} kind={filter === id ? 'primary' : 'default'} onClick={() => setFilter(id)}>
            {label}
          </Button>
        ))}
        <label className="ml-2 flex items-center gap-2 text-[13px] text-muted">
          <input type="checkbox" checked={liveOnly} onChange={(e) => setLiveOnly(e.target.checked)} />
          Live cases only
        </label>
        <span className="ml-auto text-[13px] text-muted">{entries.length} entries</span>
      </div>

      {entries.length === 0 ? (
        <Empty title="Nothing recorded yet">
          Run a payment from the Checkout screen, or inject one from Simulate.
        </Empty>
      ) : (
        <Box className="log px-5 py-2">
          {entries.map((entry, i) => {
            const kind = KIND[entry.kind] || { label: entry.kind, tone: 'text-muted' }
            return (
              <div key={i} className="border-b border-rule py-2.5 last:border-b-0">
                <div className="flex flex-wrap items-baseline gap-x-3">
                  <span className="w-[132px] shrink-0 text-muted">{when(entry.at)}</span>
                  <span className={`w-[76px] shrink-0 ${kind.tone}`}>{kind.label}</span>
                  <span className={entry.ok ? '' : 'text-halt'}>{entry.title}</span>
                  {entry.amount_paise ? (
                    <span className="text-muted">{rupees(entry.amount_paise)}</span>
                  ) : null}
                  {entry.case_id && (
                    <button
                      onClick={() => onOpenCase(entry.case_id)}
                      className="text-muted underline decoration-rule underline-offset-2 hover:text-ink"
                    >
                      case #{entry.case_id}
                    </button>
                  )}
                  {entry.ref && <span className="text-muted">{entry.ref}</span>}
                </div>

                {entry.detail && (
                  <p className="ml-[132px] mt-1 max-w-3xl whitespace-pre-wrap text-muted">
                    {entry.detail}
                  </p>
                )}

                {entry.why && (
                  <p className="ml-[132px] mt-1 max-w-3xl border-l-2 border-rule pl-3 text-muted">
                    {entry.why}
                  </p>
                )}

                {entry.kind === 'message' && (
                  <p className="ml-[132px] mt-1">
                    <DeliveryTag entry={entry} />
                  </p>
                )}

                {entry.gate_checks?.length > 0 && (
                  <div className="ml-[132px] mt-1 flex flex-wrap gap-x-4 gap-y-0.5">
                    {entry.gate_checks.map((check) => (
                      <span
                        key={check.name}
                        className={check.passed ? 'text-muted' : 'text-blocked'}
                        title={check.detail}
                      >
                        {check.passed ? '✓' : '✕'} {check.name}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </Box>
      )}
    </>
  )
}

function DeliveryTag({ entry }) {
  if (entry.delivery_status === 'sent') {
    return (
      <span className="text-recovered">
        delivered · {entry.delivery_detail}
        {entry.delivery_id ? ` · ${entry.delivery_id}` : ''}
      </span>
    )
  }
  if (entry.delivery_status === 'failed') {
    return <span className="text-halt">delivery failed · {entry.delivery_detail}</span>
  }
  return <span className="text-muted">not delivered · {entry.delivery_detail || 'rendered to the outbox only'}</span>
}

function Delivery({ status }) {
  if (!status) return null
  const on = status.deliver_for_real
  return (
    <Box className="mb-6 px-5 py-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-[13px]">
        <span>
          Real delivery{' '}
          <span className={on ? 'text-recovered' : 'text-muted'}>{on ? 'on' : 'off'}</span>
        </span>
        <span className="text-muted">
          email{' '}
          <span className={status.email_configured ? 'text-recovered' : ''}>
            {status.email_configured ? status.from_email : 'not configured'}
          </span>
        </span>
        {status.allowlist?.length > 0 && (
          <span className="text-muted">allowlist: {status.allowlist.join(', ')}</span>
        )}
      </div>
      {on && status.allowlist?.length === 0 && (
        <p className="mt-2 text-[12px] text-atrisk">
          Delivery is on with no allowlist. Every message goes to whatever address is on the case.
        </p>
      )}
    </Box>
  )
}
