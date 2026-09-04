import { useEffect, useState } from 'react'
import { api, rupeesShort } from '../api'
import { MessagesVersusRecovered, RecoveryByCause } from '../components/charts'
import { Box, Button, Empty, Metric, Panel, Spinner } from '../components/ui'

const SEED = 42
const COUNT = 3000

export default function Overview({ tick, go }) {
  const [data, setData] = useState(null)
  const [causes, setCauses] = useState({ baseline: null, router: null })
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)

  const load = async () => {
    try {
      const comparison = await api.compare(SEED, COUNT)
      setData(comparison)
      if (comparison.policies.baseline && comparison.policies.router) {
        const [b, r] = await Promise.all([
          api.byCause(`baseline-s${SEED}-n${COUNT}`),
          api.byCause(`router-s${SEED}-n${COUNT}`),
        ])
        setCauses({ baseline: b.causes, router: r.causes })
      }
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [tick])

  const runBoth = async () => {
    setRunning(true)
    try {
      await api.createRun({ policy: 'both', count: COUNT, seed: SEED })
      await load()
    } finally {
      setRunning(false)
    }
  }

  if (loading) return <Spinner />

  const baseline = data?.policies?.baseline
  const router = data?.policies?.router

  if (!baseline || !router) {
    return (
      <>
        <Header />
        <Empty
          title="No comparison has been run yet"
          action={
            <Button kind="primary" onClick={runBoth} disabled={running}>
              {running ? 'Running… (about two minutes)' : `Run both policies over ${COUNT} failures`}
            </Button>
          }
        >
          Both policies see the same {COUNT.toLocaleString('en-IN')} synthetic failures, from seed{' '}
          {SEED}. Every case is driven through the real pipeline, so the run takes roughly two
          minutes.
        </Empty>
      </>
    )
  }

  return (
    <>
      <Header />

      <Panel
        title="What the system did"
        note="Counts of real actions taken by the pipeline. No behavioural assumptions involved."
      >
        <Box className="grid grid-cols-2 gap-6 px-6 py-5 lg:grid-cols-4">
          <Metric
            label="Messages sent"
            value={router.messages_sent}
            baseline={baseline.messages_sent}
          />
          <Metric
            label="Wrong advice"
            value={router.wrong_advice_count}
            baseline={baseline.wrong_advice_count}
            tone={router.wrong_advice_count ? 'halt' : 'recovered'}
            better={baseline.wrong_advice_count > router.wrong_advice_count ? 'none' : undefined}
            hint="&ldquo;Try again&rdquo; sent to a card that is blocked for online payments"
          />
          <Metric
            label="Already-paid contacts"
            value={router.already_paid_contacts}
            baseline={baseline.already_paid_contacts}
            tone={router.already_paid_contacts ? 'halt' : 'recovered'}
            hint="Messages to customers who had already paid"
          />
          <Metric
            label="Messages suppressed"
            value={router.suppressed_count}
            baseline={baseline.suppressed_count}
            tone="blocked"
            hint="Stopped by the gate, each with a recorded reason"
          />
        </Box>
      </Panel>

      <Panel
        title="Modelled outcome"
        note="Depends on the outcome oracle in simulator.py, not on measurement. Customer intent is scripted — see fixtures/profiles_seed42.json."
      >
        <Box className="grid grid-cols-2 gap-6 px-6 py-5 lg:grid-cols-4">
          <Metric label="Amount at risk" value={rupeesShort(router.amount_at_risk_paise)} tone="atrisk" />
          <Metric
            label="Recovered by the router"
            value={rupeesShort(router.amount_recovered_paise)}
            tone="recovered"
            hint={`${router.cases_recovered} of ${router.case_count} cases`}
          />
          <Metric
            label="Recovered by the baseline"
            value={rupeesShort(baseline.amount_recovered_paise)}
            hint={`${baseline.cases_recovered} of ${baseline.case_count} cases`}
          />
          <Metric
            label="Recovery rate"
            value={`${(router.recovery_rate * 100).toFixed(1)}%`}
            baseline={`${(baseline.recovery_rate * 100).toFixed(1)}%`}
            tone="recovered"
            better={`+${((router.recovery_rate - baseline.recovery_rate) * 100).toFixed(1)} pts`}
          />
        </Box>
        <p className="mt-2 text-[12px] text-muted">
          Self-recoveries — customers who would have paid unprompted — are excluded from both
          policies&rsquo; recovered totals. {router.self_recovered_count} in this batch.
        </p>
      </Panel>

      <div className="grid gap-8 xl:grid-cols-2">
        <Panel title="Recovery by cause" note="Where the timing and the content actually matter.">
          <Box className="px-4 py-4">
            <RecoveryByCause baseline={causes.baseline} router={causes.router} />
          </Box>
        </Panel>

        <Panel title="Messages sent against cases recovered" note="Effort versus result.">
          <Box className="px-4 py-4">
            <MessagesVersusRecovered policies={data.policies} />
          </Box>
        </Panel>
      </div>

      <div className="flex gap-2">
        <Button onClick={runBoth} disabled={running}>
          {running ? 'Running… (about two minutes)' : 'Re-run both policies'}
        </Button>
        <Button kind="quiet" onClick={() => go('compare')}>
          See the full comparison
        </Button>
      </div>
    </>
  )
}

function Header() {
  return (
    <div className="mb-8 max-w-2xl">
      <h1 className="text-[19px] font-semibold tracking-[-0.02em]">Overview</h1>
      <p className="mt-1 text-[13px] text-muted">
        Razorpay&rsquo;s Failed Payment Recovery sends every failed payment the same link at the
        same time. This makes the timing and the content a function of the failure cause.
      </p>
    </div>
  )
}
