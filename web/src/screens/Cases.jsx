import { useEffect, useState } from 'react'
import { api, causeLabel, policyLabel, rupees, runLabel, when } from '../api'
import { Box, Button, Empty, Spinner, Status, Td, Th, inputClass } from '../components/ui'

const CAUSES = [
  'transient_network',
  'data_entry',
  'otp_failure',
  'gateway_degraded',
  'balance',
  'card_config',
  'issuer_decline',
  'deliberate_abandon',
  'unknown',
]

const STATUSES = [
  'detected',
  'planned',
  'acting',
  'recovered',
  'exhausted',
  'suppressed',
  'escalated',
]

export default function Cases({ tick, onOpenCase }) {
  const [cases, setCases] = useState([])
  const [runs, setRuns] = useState([])
  const [loading, setLoading] = useState(true)
  const [filters, setFilters] = useState({ root_cause: '', status: '', run_id: '' })

  useEffect(() => {
    api.runs().then((r) => setRuns(r.runs))
  }, [tick])

  useEffect(() => {
    setLoading(true)
    const params = Object.fromEntries(Object.entries(filters).filter(([, v]) => v))
    api
      .cases({ ...params, limit: 400 })
      .then((r) => setCases(r.cases))
      .finally(() => setLoading(false))
  }, [tick, filters])

  const set = (key) => (event) => setFilters({ ...filters, [key]: event.target.value })

  return (
    <>
      <div className="mb-6 max-w-2xl">
        <h1 className="text-[19px] font-semibold tracking-[-0.02em]">Cases</h1>
        <p className="mt-1 text-[13px] text-muted">
          One case per failed checkout, grouped across retries. Suppressed cases are shown, not
          hidden — deciding not to message someone is a decision worth seeing.
        </p>
      </div>

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="mb-1 block text-[12px] text-muted">Cause</span>
          <select className={inputClass} value={filters.root_cause} onChange={set('root_cause')}>
            <option value="">All causes</option>
            {CAUSES.map((c) => (
              <option key={c} value={c}>
                {causeLabel(c)}
              </option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="mb-1 block text-[12px] text-muted">Status</span>
          <select className={inputClass} value={filters.status} onChange={set('status')}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="mb-1 block text-[12px] text-muted">Batch</span>
          <select className={inputClass} value={filters.run_id} onChange={set('run_id')}>
            <option value="">All batches</option>
            <option value="live">Live and injected</option>
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {runLabel(r.run_id)}
              </option>
            ))}
          </select>
        </label>

        {(filters.root_cause || filters.status || filters.run_id) && (
          <Button kind="quiet" onClick={() => setFilters({ root_cause: '', status: '', run_id: '' })}>
            Clear
          </Button>
        )}

        <span className="ml-auto text-[13px] text-muted">{cases.length} cases</span>
      </div>

      {loading ? (
        <Spinner />
      ) : cases.length === 0 ? (
        <Empty title="No cases match these filters">
          Inject a failed payment from the Simulate screen, or run a policy comparison.
        </Empty>
      ) : (
        <Box className="overflow-x-auto px-5 py-1">
          <table className="w-full min-w-[820px] text-[13px]">
            <thead>
              <tr>
                <Th right>Amount</Th>
                <Th>Cause</Th>
                <Th>Customer</Th>
                <Th>Status</Th>
                <Th>Next action</Th>
                <Th>Failed</Th>
                <Th>Policy</Th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr
                  key={c.id}
                  onClick={() => onOpenCase(c.id)}
                  className={`cursor-pointer hover:bg-[#f6f5f1] ${
                    c.status === 'suppressed' ? 'text-blocked' : ''
                  }`}
                >
                  <Td right>{rupees(c.amount_paise)}</Td>
                  <Td>
                    <span>{causeLabel(c.root_cause)}</span>
                    <div className="text-[11px] text-muted">{c.rule_id || c.error_reason}</div>
                  </Td>
                  <Td>{c.customer_name || '—'}</Td>
                  <Td>
                    <Status value={c.status} />
                  </Td>
                  <Td>
                    {c.next_action_at ? (
                      <>
                        <span className="num">
                          {when(c.next_action_at)}
                          <span className="ml-1.5 text-muted">{c.next_action_channel}</span>
                        </span>
                        {/* A 2nd message is a day out by design, and a deferred
                            one is off the rule's schedule entirely. Saying so
                            stops either looking like a broken delay. */}
                        {(c.next_action_index > 1 || c.next_action_deferred) && (
                          <div className="text-[11px] text-muted">
                            {c.next_action_index > 1 && `follow-up (message ${c.next_action_index})`}
                            {c.next_action_index > 1 && c.next_action_deferred && ' · '}
                            {c.next_action_deferred && `held: ${c.next_action_deferred}`}
                          </div>
                        )}
                      </>
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </Td>
                  <Td className="num text-muted">{when(c.failed_at)}</Td>
                  <Td className="text-muted">{policyLabel(c.policy)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </Box>
      )}
    </>
  )
}
