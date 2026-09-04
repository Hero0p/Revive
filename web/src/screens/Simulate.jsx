import { useEffect, useState } from 'react'
import { api, causeLabel, when } from '../api'
import { Box, Button, Empty, Field, Panel, Status, Td, Th, inputClass } from '../components/ui'

const REASONS = [
  ['payment_timed_out', 'Network timeout'],
  ['card_number_invalid', 'Card number typo'],
  ['authentication_failed', 'OTP / authentication failed'],
  ['gateway_technical_error', 'Gateway or bank outage'],
  ['insufficient_fund', 'Insufficient funds'],
  ['card_disabled_for_online_payments', 'Card blocked for online payments'],
  ['card_declined', 'Bank declined'],
  ['payment_cancelled', 'Customer cancelled'],
  ['a_reason_we_have_never_seen', 'An unmapped reason (escalates)'],
]

export default function Simulate({ tick, onChange, onOpenCase }) {
  const [form, setForm] = useState({
    error_reason: 'insufficient_fund',
    amount_rupees: 4000,
    customer_name: 'Aarav Sharma',
    language: 'en',
    payday_days: '1',
    opted_out: false,
  })
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [live, setLive] = useState([])
  const [chaos, setChaos] = useState(null)
  const [events, setEvents] = useState([])

  const reload = async () => {
    const [cases, chaosState, rawEvents] = await Promise.all([
      api.cases({ run_id: 'live', limit: 40 }),
      api.chaos(),
      api.events(),
    ])
    setLive(cases.cases)
    setChaos(chaosState)
    setEvents(rawEvents.events.slice(0, 6))
  }

  useEffect(() => {
    reload()
  }, [tick])

  const set = (key) => (e) =>
    setForm({ ...form, [key]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })

  const inject = async () => {
    setBusy(true)
    try {
      const payday = form.payday_days
        .split(',')
        .map((d) => parseInt(d.trim(), 10))
        .filter((d) => d >= 1 && d <= 31)
      const response = await api.inject({
        error_reason: form.error_reason,
        amount_paise: Math.round(Number(form.amount_rupees) * 100),
        customer_name: form.customer_name,
        language: form.language,
        payday_days: payday,
        opted_out: form.opted_out,
        contact: '+9198' + Math.floor(10000000 + Math.random() * 89999999),
        email: form.customer_name.split(' ')[0].toLowerCase() + '@example.com',
      })
      setResult(response)
      onChange()
      await reload()
    } finally {
      setBusy(false)
    }
  }

  const toggleChaos = async (key) => {
    await api.setChaos({ [key]: !chaos?.[key === 'razorpay_down' ? 'razorpay' : 'llm_down'] })
    onChange()
    await reload()
  }

  return (
    <>
      <div className="mb-6 max-w-2xl">
        <h1 className="text-[19px] font-semibold tracking-[-0.02em]">Simulate</h1>
        <p className="mt-1 text-[13px] text-muted">
          Injected failures are synthetic payloads sent through the real webhook route — signed,
          signature-verified, stored in raw_events, and processed by the same pipeline as live
          Razorpay traffic.
        </p>
      </div>

      <div className="grid gap-8 lg:grid-cols-[380px_1fr]">
        <div>
          <Panel title="Inject a failed payment">
            <Box className="space-y-4 px-5 py-5">
              <Field label="Failure reason">
                <select className={inputClass} value={form.error_reason} onChange={set('error_reason')}>
                  {REASONS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </Field>

              <div className="grid grid-cols-2 gap-3">
                <Field label="Amount (₹)">
                  <input
                    className={inputClass + ' num'}
                    type="number"
                    value={form.amount_rupees}
                    onChange={set('amount_rupees')}
                  />
                </Field>
                <Field label="Language">
                  <select className={inputClass} value={form.language} onChange={set('language')}>
                    <option value="en">English</option>
                    <option value="hi">Hindi</option>
                    <option value="hinglish">Hinglish</option>
                  </select>
                </Field>
              </div>

              <Field label="Customer">
                <input className={inputClass} value={form.customer_name} onChange={set('customer_name')} />
              </Field>

              <Field label="Past payday days (day of month, comma separated)">
                <input
                  className={inputClass + ' num'}
                  value={form.payday_days}
                  onChange={set('payday_days')}
                  placeholder="1, 1, 28"
                />
              </Field>

              <label className="flex items-center gap-2 text-[13px]">
                <input type="checkbox" checked={form.opted_out} onChange={set('opted_out')} />
                Customer has opted out
              </label>

              <Button kind="primary" onClick={inject} disabled={busy} className="w-full">
                {busy ? 'Sending…' : 'Send through the webhook'}
              </Button>

              {result && (
                <div className="log border-t border-rule pt-3 text-muted">
                  <div>POST {result.sent_through}</div>
                  <div>raw_event #{result.raw_event_id} · signature verified</div>
                  <div>
                    case #{result.case_id} · <Status value={result.status} />
                  </div>
                </div>
              )}
            </Box>
          </Panel>

          <Panel title="Failure modes" note="Each one is handled, logged, and recoverable.">
            <Box className="space-y-3 px-5 py-4">
              <Toggle
                label="Razorpay API down"
                on={chaos?.razorpay?.chaos_razorpay_down}
                detail={
                  chaos
                    ? `breaker ${chaos.razorpay.breaker} · ${chaos.razorpay.consecutive_failures} consecutive failures` +
                    (chaos.razorpay.breaker === 'open'
                      ? ` · reopens in ${chaos.razorpay.reopens_in_seconds}s`
                      : '') +
                    (chaos.razorpay.last_error ? ` · last: ${chaos.razorpay.last_error}` : '')
                    : ''
                }
                onClick={() => toggleChaos('razorpay_down')}
              />
              {chaos?.razorpay?.breaker !== 'closed' && (
                <Button
                  kind="quiet"
                  onClick={async () => {
                    await api.setChaos({ reset: true })
                    onChange()
                    await reload()
                  }}
                >
                  Force-reset the breaker
                </Button>
              )}
              <Toggle
                label="LLM down"
                on={chaos?.llm_down}
                detail={
                  chaos?.llm_configured
                    ? 'falls back to hand-written templates'
                    : 'no API key set, templates are already in use'
                }
                onClick={() => toggleChaos('llm_down')}
              />
              <div className="flex items-center justify-between gap-3 border-t border-rule pt-3">
                <div>
                  <div className="text-[13px]">Tampered webhook</div>
                  <div className="text-[12px] text-muted">Bad signature, rejected and logged</div>
                </div>
                <Button
                  onClick={async () => {
                    await api.tamper()
                    onChange()
                    await reload()
                  }}
                >
                  Send one
                </Button>
              </div>
            </Box>
          </Panel>
        </div>

        <div>
          <Panel title="Live cases" note="Everything injected here, newest first.">
            {live.length === 0 ? (
              <Empty title="Nothing injected yet">
                Send a failed payment and it appears here with its scheduled action.
              </Empty>
            ) : (
              <Box className="overflow-x-auto px-5 py-1">
                <table className="w-full min-w-[560px] text-[13px]">
                  <thead>
                    <tr>
                      <Th>Cause</Th>
                      <Th>Status</Th>
                      <Th>Next action</Th>
                      <Th>Failed</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {live.map((c) => (
                      <tr
                        key={c.id}
                        onClick={() => onOpenCase(c.id)}
                        className="cursor-pointer hover:bg-[#f6f5f1]"
                      >
                        <Td>
                          {causeLabel(c.root_cause)}
                          <div className="text-[11px] text-muted">{c.rule_id}</div>
                        </Td>
                        <Td>
                          <Status value={c.status} />
                        </Td>
                        <Td className="num">
                          {c.next_action_at ? when(c.next_action_at) : '—'}
                          {c.next_action_channel && (
                            <span className="ml-1.5 text-muted">{c.next_action_channel}</span>
                          )}
                        </Td>
                        <Td className="num text-muted">{when(c.failed_at)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Box>
            )}
          </Panel>

          <Panel title="Raw events" note="Every webhook is stored before it is processed.">
            <Box className="log px-5 py-3">
              {events.map((e) => (
                <div key={e.id} className="flex flex-wrap gap-x-4 border-b border-rule py-1.5 last:border-b-0">
                  <span className="text-muted">#{e.id}</span>
                  <span>{e.event_type}</span>
                  <span className="text-muted">{when(e.received_at)}</span>
                  {e.error ? (
                    <span className="text-halt">{e.error}</span>
                  ) : (
                    <span className="text-recovered">processed</span>
                  )}
                </div>
              ))}
              {events.length === 0 && <span className="text-muted">No events yet.</span>}
            </Box>
          </Panel>
        </div>
      </div>
    </>
  )
}

function Toggle({ label, on, detail, onClick }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <div>
        <div className="text-[13px]">{label}</div>
        <div className="text-[12px] text-muted">{detail}</div>
      </div>
      <Button onClick={onClick} kind={on ? 'primary' : 'default'}>
        {on ? 'On' : 'Off'}
      </Button>
    </div>
  )
}
