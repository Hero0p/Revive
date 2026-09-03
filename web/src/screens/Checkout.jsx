import { useEffect, useState } from 'react'
import { api, rupees, when } from '../api'
import { Box, Button, Field, Panel, Status, inputClass } from '../components/ui'

const CART = [
  { name: 'Attikan Estate Coffee 250g', price_paise: 65000 },
  { name: 'Ceramic Pour-Over Dripper', price_paise: 145000 },
]
const TOTAL = CART.reduce((sum, item) => sum + item.price_paise, 0)

const readable = (reason) => reason.replace(/_/g, ' ')

/** Loads Razorpay's checkout script once, on demand. */
function loadCheckout() {
  return new Promise((resolve, reject) => {
    if (window.Razorpay) return resolve(window.Razorpay)
    const script = document.createElement('script')
    script.src = 'https://checkout.razorpay.com/v1/checkout.js'
    script.onload = () => resolve(window.Razorpay)
    script.onerror = () => reject(new Error('could not load the Razorpay checkout script'))
    document.body.appendChild(script)
  })
}

export default function Checkout({ onChange, onOpenCase }) {
  const [form, setForm] = useState({
    name: 'Aarav Sharma',
    contact: '+919812345678',
    // Razorpay's checkout modal can auto-fill a returning phone number's
    // previously-used email from its own saved records, overriding whatever
    // is prefilled here -- this default just removes one place the wrong
    // address could otherwise creep in from.
    email: 'nishantc3110@gmail.com',
  })
  const [log, setLog] = useState([])
  const [busy, setBusy] = useState(false)
  const [health, setHealth] = useState(null)
  const [synced, setSynced] = useState(null)
  const [reasonOptions, setReasonOptions] = useState([])
  const [reasonPicks, setReasonPicks] = useState({})
  const [reclassifying, setReclassifying] = useState(null)
  const [thisCheckoutCase, setThisCheckoutCase] = useState(null)

  useEffect(() => {
    api.health().then(setHealth)
    // Every reason the decision table can act on, except the ambiguous
    // payment_failed catch-all -- reclassifying a case is only meaningful
    // when it lands on a rule.
    api.rules().then((r) => {
      setReasonOptions(r.rules.map((x) => x.error_reason).filter((x) => x !== 'payment_failed'))
    })
  }, [])

  const note = (text, tone = 'ink') =>
    setLog((entries) => [...entries, { text, tone, at: new Date().toISOString() }])

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value })

  const runSync = async () => {
    const result = await api.syncRazorpay()
    setSynced(result)
    note(
      `Fetched ${result.fetched} payments from Razorpay, ingested ${result.ingested_failures} failures.`,
      result.ingested_failures ? 'recovered' : 'muted',
    )
    onChange()
    return result
  }

  /** The case for THIS checkout's order, however it got ingested.

  Sync's response only ever lists payments it just newly ingested -- a
  payment already known (because a real webhook delivered it first, which
  happens whenever a tunnel is connected) is silently skipped from that
  response even though the case exists. Looking the case up directly by
  order_id finds it either way. */
  const findThisCase = async (orderId) => {
    const { cases } = await api.cases({ run_id: 'live', limit: 30 })
    return cases.find((c) => c.order_id === orderId) || null
  }

  const pay = async () => {
    setBusy(true)
    setLog([])
    setSynced(null)
    setThisCheckoutCase(null)
    try {
      note(`Creating a real test-mode order for ${rupees(TOTAL)}…`)
      const { order, key_id, live } = await api.createOrder({
        amount_paise: TOTAL,
        cart: CART,
        customer_name: form.name,
        contact: form.contact,
        email: form.email,
      })
      if (!live) {
        note('No Razorpay keys configured, so this order is simulated.', 'atrisk')
        setBusy(false)
        return
      }
      note(`Order ${order.id} created on Razorpay.`, 'recovered')

      const Razorpay = await loadCheckout()
      note('Opening the Razorpay checkout modal.')

      const rzp = new Razorpay({
        key: key_id,
        order_id: order.id,
        amount: order.amount,
        currency: order.currency,
        name: 'Blue Tokai Coffee',
        description: CART.map((i) => i.name).join(', '),
        prefill: { name: form.name, contact: form.contact, email: form.email },
        // These ride along to the payment entity, so a failure arrives with a
        // real cart and a real customer attached.
        notes: {
          cart: JSON.stringify(CART),
          customer_name: form.name,
          language: 'en',
        },
        theme: { color: '#0F7B4F' },
        modal: {
          ondismiss: () => {
            note('You closed the modal. Razorpay records that as a cancelled payment.', 'blocked')
            setBusy(false)
          },
        },
        handler: async (response) => {
          note(`Payment ${response.razorpay_payment_id} succeeded. Verifying the signature…`)
          try {
            const verified = await api.verifyPayment(response)
            note(
              `Signature verified. Case ${verified.case_id ?? '—'} is now ${verified.case_status ?? 'closed'}.`,
              'recovered',
            )
          } catch (err) {
            note(`Verification failed: ${err.message}. Nothing was marked as paid.`, 'halt')
          }
          setBusy(false)
          onChange()
        },
      })

      rzp.on('payment.failed', async (event) => {
        const e = event.error || {}
        note(
          `Payment failed: ${e.reason || 'unknown'} (${e.code || 'no code'}) — ${e.description || ''}`,
          'halt',
        )
        note('Checking whether the case has reached the pipeline…')

        // A real webhook (if a tunnel is connected) usually delivers this
        // before we ever ask, so sync's response cannot be trusted to list
        // it -- look the case up directly instead. Still try sync first in
        // case no webhook is connected and the Payments API is the only path
        // in.
        try {
          await runSync()
        } catch (err) {
          note(`Sync skipped: ${err.message}`, 'muted')
        }

        try {
          let found = await findThisCase(order.id)
          if (!found) {
            // The webhook can lag by a second or two behind the client-side
            // event. One short retry before giving up.
            await new Promise((r) => setTimeout(r, 1500))
            found = await findThisCase(order.id)
          }
          if (found) {
            note(`Case #${found.id}: ${found.error_reason} → ${found.status}`, 'ink')
            if (found.status === 'escalated') {
              setThisCheckoutCase(found)
              note(
                "Razorpay's response carried no reason the decision table recognises (see " +
                  'fixtures/captured/README.md — this is routine in test mode). Assign one below ' +
                  'to continue the demo.',
                'atrisk',
              )
            }
          } else {
            note(
              'Case not found yet. It may still be arriving -- try "Pull recent payments" below in ' +
                'a moment.',
              'muted',
            )
          }
        } catch (err) {
          note(err.message, 'halt')
        }
        setBusy(false)
      })

      rzp.open()
    } catch (err) {
      note(err.message, 'halt')
      setBusy(false)
    }
  }

  const sync = async () => {
    setBusy(true)
    try {
      await runSync()
    } catch (err) {
      note(err.message, 'halt')
    } finally {
      setBusy(false)
    }
  }

  const reclassify = async (caseId, body) => {
    setReclassifying(caseId)
    try {
      const result = await api.reclassify(caseId, body)
      note(
        `Case #${caseId} reclassified to ${readable(result.error_reason)} (${result.rule_id}), ` +
          `scheduled for ${when(result.next_action_at)}.`,
        'recovered',
      )
      setSynced((prev) =>
        prev
          ? {
              ...prev,
              cases: prev.cases.map((c) =>
                c.case_id === caseId ? { ...c, case_status: result.status } : c,
              ),
            }
          : prev,
      )
      setThisCheckoutCase((prev) => (prev?.id === caseId ? null : prev))
      onChange()
    } catch (err) {
      note(`Could not reclassify case #${caseId}: ${err.message}`, 'halt')
    } finally {
      setReclassifying(null)
    }
  }

  return (
    <>
      <div className="mb-6 max-w-2xl">
        <h1 className="text-[19px] font-semibold tracking-[-0.02em]">Checkout</h1>
        <p className="mt-1 text-[13px] text-muted">
          A real Razorpay test-mode checkout. Pay and fail here, and the failure becomes a case in
          the pipeline with a rule, a scheduled message, and an audit trail.
        </p>
      </div>

      {health && !health.razorpay_live && (
        <Box className="mb-6 px-5 py-3 text-[13px] text-atrisk">
          No Razorpay keys are configured, so this screen cannot create a real order. Add
          RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET to .env and restart the API.
        </Box>
      )}

      <div className="grid gap-8 lg:grid-cols-[400px_1fr]">
        <div>
          <Panel title="The cart">
            <Box className="px-5 py-4">
              <table className="mb-4 w-full text-[13px]">
                <tbody>
                  {CART.map((item) => (
                    <tr key={item.name} className="border-b border-rule">
                      <td className="py-2">{item.name}</td>
                      <td className="num py-2 text-right">{rupees(item.price_paise)}</td>
                    </tr>
                  ))}
                  <tr>
                    <td className="py-2 font-medium">Total</td>
                    <td className="num py-2 text-right font-medium">{rupees(TOTAL)}</td>
                  </tr>
                </tbody>
              </table>

              <div className="space-y-3">
                <Field label="Name">
                  <input className={inputClass} value={form.name} onChange={set('name')} />
                </Field>
                <Field label="Contact">
                  <input className={inputClass} value={form.contact} onChange={set('contact')} />
                </Field>
                <Field label="Email">
                  <input className={inputClass} value={form.email} onChange={set('email')} />
                </Field>
              </div>

              <Button
                kind="primary"
                onClick={pay}
                disabled={busy || (health && !health.razorpay_live)}
                className="mt-4 w-full"
              >
                {busy ? 'Working…' : `Pay ${rupees(TOTAL)}`}
              </Button>
            </Box>
          </Panel>

          <Panel title="How to make it fail">
            <Box className="px-5 py-4 text-[13px] text-muted">
              <p className="mb-2">
                In test mode Razorpay never charges anything. To produce a real failed payment:
              </p>
              <ul className="ml-4 list-disc space-y-1">
                <li>
                  Pick <span className="text-ink">Netbanking</span> or{' '}
                  <span className="text-ink">UPI</span> — test mode shows a simulated bank page with
                  an explicit <span className="text-ink">Failure</span> option.
                </li>
                <li>
                  Or use a card from Razorpay&rsquo;s{' '}
                  <a
                    className="underline decoration-rule underline-offset-2 hover:text-ink"
                    href="https://razorpay.com/docs/payments/payments/test-card-details/"
                    target="_blank"
                    rel="noreferrer"
                  >
                    test card list
                  </a>
                  , which has one card per error scenario.
                </li>
                <li>
                  Closing the modal produces a cancelled payment, which maps to rule
                  R8_USER_CANCELLED.
                </li>
              </ul>
              <p className="mt-3">
                Whatever reason comes back, real test-mode traffic is usually the generic{' '}
                <span className="text-ink">payment_failed</span> catch-all with no specific cause
                attached (see <span className="text-ink">fixtures/captured/README.md</span>). When
                that happens the case escalates, and you can assign a cause for the demo below
                rather than the pipeline guessing one.
              </p>
            </Box>
          </Panel>
        </div>

        <div>
          <Panel
            title="What just happened"
            note="Each step of the real Razorpay round trip, in order."
          >
            <Box className="log px-5 py-4">
              {log.length === 0 ? (
                <span className="text-muted">
                  Nothing yet. Press Pay to create a real test-mode order.
                </span>
              ) : (
                <ol className="space-y-1.5">
                  {log.map((entry, i) => (
                    <li key={i} className="flex gap-3">
                      <span className="num shrink-0 text-muted">
                        {when(entry.at, { withDate: false })}
                      </span>
                      <span
                        className={
                          {
                            recovered: 'text-recovered',
                            halt: 'text-halt',
                            atrisk: 'text-atrisk',
                            blocked: 'text-blocked',
                            muted: 'text-muted',
                            ink: '',
                          }[entry.tone]
                        }
                      >
                        {entry.text}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </Box>
          </Panel>

          {thisCheckoutCase && (
            <Panel
              title="Assign a cause for this demo"
              note="This case escalated because Razorpay's real response carried nothing the decision table recognises. A human picks the cause here -- the pipeline never does."
            >
              <Box className="px-5 py-4">
                <div className="mb-3 text-[13px]">
                  <button
                    onClick={() => onOpenCase(thisCheckoutCase.id)}
                    className="text-ink underline decoration-rule underline-offset-2 hover:no-underline"
                  >
                    Case #{thisCheckoutCase.id}
                  </button>{' '}
                  <span className="text-muted">
                    · {rupees(thisCheckoutCase.amount_paise)} · real reason was &ldquo;
                    {thisCheckoutCase.error_reason}&rdquo;
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    className="rounded border border-rule bg-white px-2 py-1.5 text-[13px]"
                    value={reasonPicks[thisCheckoutCase.id] || reasonOptions[0] || ''}
                    onChange={(e) =>
                      setReasonPicks((p) => ({ ...p, [thisCheckoutCase.id]: e.target.value }))
                    }
                    disabled={reclassifying === thisCheckoutCase.id}
                  >
                    {reasonOptions.map((r) => (
                      <option key={r} value={r}>
                        {readable(r)}
                      </option>
                    ))}
                  </select>
                  <Button
                    kind="primary"
                    onClick={() =>
                      reclassify(thisCheckoutCase.id, {
                        error_reason: reasonPicks[thisCheckoutCase.id] || reasonOptions[0],
                      })
                    }
                    disabled={reclassifying === thisCheckoutCase.id || reasonOptions.length === 0}
                  >
                    Assign
                  </Button>
                  <Button
                    onClick={() => reclassify(thisCheckoutCase.id, { random: true })}
                    disabled={reclassifying === thisCheckoutCase.id}
                  >
                    {reclassifying === thisCheckoutCase.id ? 'Working…' : 'Random'}
                  </Button>
                </div>
              </Box>
            </Panel>
          )}

          <Panel
            title="Sync from Razorpay"
            note="Razorpay only delivers webhooks to a public URL. This polls the Payments API instead and pushes each real failure through the same signed webhook route."
          >
            <Box className="px-5 py-4">
              <Button onClick={sync} disabled={busy}>
                Pull recent payments
              </Button>

              {synced && (
                <div className="log mt-4 space-y-3 border-t border-rule pt-3">
                  <div className="text-muted">
                    fetched {synced.fetched} · ingested {synced.ingested_failures} failures ·
                    skipped {synced.skipped}
                  </div>
                  {synced.cases.map((c) => (
                    <div key={c.payment_id}>
                      <button
                        onClick={() => onOpenCase(c.case_id)}
                        className="block text-left hover:text-ink"
                      >
                        <span className="text-muted">{c.payment_id}</span>{' '}
                        <span className="text-ink">{c.error_reason || 'no reason given'}</span>{' '}
                        <span className="text-muted">→ case #{c.case_id}</span>{' '}
                        <Status value={c.case_status} />
                      </button>

                      {c.case_status === 'escalated' && (
                        <div className="mt-1.5 flex flex-wrap items-center gap-2 border-l-2 border-atrisk pl-3">
                          <span className="text-muted">no rule matched — assign one:</span>
                          <select
                            className="rounded border border-rule bg-white px-1.5 py-1 text-[12px]"
                            value={reasonPicks[c.case_id] || reasonOptions[0] || ''}
                            onChange={(e) =>
                              setReasonPicks((p) => ({ ...p, [c.case_id]: e.target.value }))
                            }
                            disabled={reclassifying === c.case_id}
                          >
                            {reasonOptions.map((r) => (
                              <option key={r} value={r}>
                                {readable(r)}
                              </option>
                            ))}
                          </select>
                          <Button
                            onClick={() =>
                              reclassify(c.case_id, {
                                error_reason: reasonPicks[c.case_id] || reasonOptions[0],
                              })
                            }
                            disabled={reclassifying === c.case_id || reasonOptions.length === 0}
                          >
                            Assign
                          </Button>
                          <Button
                            kind="quiet"
                            onClick={() => reclassify(c.case_id, { random: true })}
                            disabled={reclassifying === c.case_id}
                          >
                            {reclassifying === c.case_id ? 'Working…' : 'Random'}
                          </Button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </Box>
          </Panel>
        </div>
      </div>
    </>
  )
}
