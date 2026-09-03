import { useEffect, useState } from 'react'
import { api, causeLabel } from '../api'
import { Box, Panel, Spinner } from '../components/ui'

/** The decision table, read straight from rules.py. What you see here is the
 *  code that runs -- there is no second copy. */
export default function Rules() {
  const [rules, setRules] = useState(null)

  useEffect(() => {
    api.rules().then((r) => setRules(r.rules))
  }, [])

  if (!rules) return <Spinner />

  return (
    <>
      <div className="mb-6 max-w-2xl">
        <h1 className="text-[19px] font-semibold tracking-[-0.02em]">Decision table</h1>
        <p className="mt-1 text-[13px] text-muted">
          Eight failure reasons, eight responses. Read directly from rules.py, so this is the code
          that runs rather than a description of it. Nothing here is learned or inferred.
        </p>
      </div>

      <Panel>
        <div className="space-y-3">
          {rules.map((rule) => (
            <Box key={rule.rule_id} className="px-5 py-4">
              <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
                <span className="log font-medium">{rule.rule_id}</span>
                <span className="text-[13px]">{causeLabel(rule.root_cause)}</span>
                <span className="log text-muted">{rule.error_reason}</span>
              </div>

              <p className="mb-3 max-w-3xl text-[13px]">{rule.why}</p>

              <div className="log flex flex-wrap gap-x-5 gap-y-1 border-t border-rule pt-2 text-muted">
                <span>
                  wait{' '}
                  <span className="text-ink">
                    {rule.delay_strategy || formatDelay(rule.delay_minutes)}
                  </span>
                </span>
                <span>
                  on <span className="text-ink">{rule.channel}</span>
                </span>
                <span>
                  at most <span className="text-ink">{rule.max_messages}</span>{' '}
                  {rule.max_messages === 1 ? 'message' : 'messages'}
                </span>
                <span>
                  assumed conversion <span className="text-ink">{rule.base_conversion}</span>
                </span>
                {rule.suggests_alt_method && (
                  <span className="text-recovered">offers another payment method</span>
                )}
                {!rule.mention_reason && (
                  <span className="text-atrisk">never states the reason</span>
                )}
              </div>
            </Box>
          ))}
        </div>
      </Panel>

      <p className="max-w-3xl text-[13px] text-muted">
        A failure reason that is not in this table is never guessed at. The case is created with an
        unknown cause, marked escalated, and put in front of a human.
      </p>
    </>
  )
}

function formatDelay(minutes) {
  if (minutes == null) return '—'
  if (minutes < 60) return `${minutes} minutes`
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} hours`
  return `${Math.round(minutes / 1440)} days`
}
