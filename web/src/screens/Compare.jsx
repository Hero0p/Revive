import { useEffect, useState } from 'react'
import { api, rupees, rupeesShort } from '../api'
import { MoneyByCause } from '../components/charts'
import { Box, Button, Empty, Panel, Spinner, Td, Th, inputClass } from '../components/ui'

export default function Compare({ tick, onChange }) {
  const [seed, setSeed] = useState(42)
  const [count, setCount] = useState(3000)
  const [data, setData] = useState(null)
  const [causes, setCauses] = useState(null)
  const [sweep, setSweep] = useState(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    try {
      const comparison = await api.compare(seed, count)
      setData(comparison)
      if (comparison.policies.router) {
        setCauses((await api.byCause(`router-s${seed}-n${count}`)).causes)
      }
      const runs = await api.runs()
      const stored = runs.runs.find((r) => r.run_id === `router-s${seed}-n${count}`)
      setSweep(stored?.extra?.sweep || null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [tick])

  const run = async () => {
    setBusy(true)
    try {
      await api.createRun({ policy: 'both', count: Number(count), seed: Number(seed) })
      await load()
      onChange()
    } finally {
      setBusy(false)
    }
  }

  const runSweep = async () => {
    setBusy(true)
    try {
      setSweep(await api.sweep({ policy: 'both', count: Number(count), seed: Number(seed) }))
    } finally {
      setBusy(false)
    }
  }

  const baseline = data?.policies?.baseline
  const router = data?.policies?.router

  return (
    <>
      <div className="mb-6 max-w-2xl">
        <h1 className="text-[19px] font-semibold tracking-[-0.02em]">Compare</h1>
        <p className="mt-1 text-[13px] text-muted">
          Both policies see the same failures, from the same seed. The outcome oracle is never told
          which policy produced an action.
        </p>
      </div>

      <div className="mb-6 flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="mb-1 block text-[12px] text-muted">Seed</span>
          <input
            className={inputClass + ' num w-24'}
            value={seed}
            onChange={(e) => setSeed(e.target.value)}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-[12px] text-muted">Failures</span>
          <input
            className={inputClass + ' num w-24'}
            value={count}
            onChange={(e) => setCount(e.target.value)}
          />
        </label>
        <Button kind="primary" onClick={run} disabled={busy}>
          {busy ? 'Running…' : 'Run both policies'}
        </Button>
        <Button onClick={runSweep} disabled={busy}>
          Run the sensitivity sweep
        </Button>
      </div>

      {loading ? (
        <Spinner />
      ) : !baseline || !router ? (
        <Empty title={`No runs for seed ${seed} at ${count} failures`}>
          Run both policies to produce the comparison.
        </Empty>
      ) : (
        <>
          <Panel
            title="What the system did"
            note="Facts about actions taken. Stronger evidence than anything modelled, so it comes first."
          >
            <Box className="overflow-x-auto px-5 py-1">
              <table className="w-full min-w-[560px] text-[13px]">
                <thead>
                  <tr>
                    <Th>Metric</Th>
                    <Th right>Baseline</Th>
                    <Th right>Router</Th>
                    <Th>&nbsp;</Th>
                  </tr>
                </thead>
                <tbody>
                  <Row label="Messages sent" a={baseline.messages_sent} b={router.messages_sent} />
                  <Row
                    label="Wrong advice"
                    a={baseline.wrong_advice_count}
                    b={router.wrong_advice_count}
                    note="&ldquo;Try again&rdquo; to a card blocked for online payments"
                    lowerIsBetter
                  />
                  <Row
                    label="Already-paid contacts"
                    a={baseline.already_paid_contacts}
                    b={router.already_paid_contacts}
                    note="Messages to customers who had already paid"
                    lowerIsBetter
                  />
                  <Row
                    label="Messages suppressed"
                    a={baseline.suppressed_count}
                    b={router.suppressed_count}
                    note="Stopped by the gate, with a recorded reason"
                  />
                  <Row
                    label="Escalated to a human"
                    a={baseline.escalated_count}
                    b={router.escalated_count}
                    note="Unmapped failure reasons, never guessed"
                  />
                </tbody>
              </table>
            </Box>
          </Panel>

          <Panel
            title="Modelled outcome"
            note="Produced by the outcome oracle. Customer intent is scripted, so this is a model, not a measurement."
          >
            <Box className="overflow-x-auto px-5 py-1">
              <table className="w-full min-w-[560px] text-[13px]">
                <thead>
                  <tr>
                    <Th>Metric</Th>
                    <Th right>Baseline</Th>
                    <Th right>Router</Th>
                    <Th>&nbsp;</Th>
                  </tr>
                </thead>
                <tbody>
                  <Row
                    label="Amount at risk"
                    a={rupeesShort(baseline.amount_at_risk_paise)}
                    b={rupeesShort(router.amount_at_risk_paise)}
                    plain
                  />
                  <Row
                    label="Amount recovered"
                    a={rupeesShort(baseline.amount_recovered_paise)}
                    b={rupeesShort(router.amount_recovered_paise)}
                    plain
                  />
                  <Row
                    label="Recovery rate"
                    a={`${(baseline.recovery_rate * 100).toFixed(1)}%`}
                    b={`${(router.recovery_rate * 100).toFixed(1)}%`}
                    note={`+${((router.recovery_rate - baseline.recovery_rate) * 100).toFixed(1)} points`}
                    plain
                  />
                  <Row
                    label="Recovered without any message"
                    a={baseline.self_recovered_count}
                    b={router.self_recovered_count}
                    note="Excluded from both policies' recovered totals"
                  />
                </tbody>
              </table>
            </Box>
          </Panel>

          {sweep && (
            <Panel
              title="Sensitivity sweep"
              note="Intent decay, channel response, and the payday penalty moved together. The claim is directional robustness, not one number."
            >
              <Box className="px-5 py-4">
                <p className="text-[15px]">
                  The router wins{' '}
                  <span className="num font-semibold text-recovered">
                    {sweep.router_wins} of {sweep.settings_tested}
                  </span>{' '}
                  parameter settings.
                </p>
                <div className="log mt-3 max-h-56 overflow-y-auto">
                  <table className="w-full">
                    <tbody>
                      {sweep.results.map((r, i) => (
                        <tr key={i} className="border-b border-rule last:border-b-0">
                          <td className="py-1 text-muted">decay ×{r.intent_decay_scale}</td>
                          <td className="py-1 text-muted">channel ×{r.channel_response_scale}</td>
                          <td className="py-1 text-muted">payday {r.payday_penalty}</td>
                          <td className="py-1 text-right">{rupees(r.baseline_paise)}</td>
                          <td className="py-1 text-right">{rupees(r.router_paise)}</td>
                          <td
                            className={`py-1 pl-4 ${r.router_wins ? 'text-recovered' : 'text-atrisk'}`}
                          >
                            {r.router_wins ? 'router' : 'baseline'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Box>
            </Panel>
          )}

          {causes && (
            <Panel title="Where the money is" note="Router run, by failure cause.">
              <Box className="px-4 py-4">
                <MoneyByCause causes={causes} />
              </Box>
            </Panel>
          )}
        </>
      )}
    </>
  )
}

function Row({ label, a, b, note, lowerIsBetter, plain }) {
  const better = !plain && lowerIsBetter && Number(b) < Number(a)
  return (
    <tr>
      <Td>{label}</Td>
      <Td right>{a}</Td>
      <Td right className={better ? 'text-recovered' : ''}>
        {b}
      </Td>
      <Td className="text-[12px] text-muted">{note}</Td>
    </tr>
  )
}
