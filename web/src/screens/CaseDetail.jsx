import { useEffect, useState } from 'react'
import { api, causeLabel, rupees, when } from '../api'
import Timeline from '../components/Timeline'
import { Box, Button, Panel, Spinner, Status } from '../components/ui'

export default function CaseDetail({ caseId, tick, clock, onBack }) {
  const [detail, setDetail] = useState(null)
  const [timeline, setTimeline] = useState(null)

  useEffect(() => {
    if (!caseId) return
    api.caseDetail(caseId).then(setDetail)
    api.timeline(caseId).then(setTimeline)
  }, [caseId, tick])

  if (!detail) return <Spinner />

  return (
    <>
      <Button kind="quiet" onClick={onBack} className="mb-4 -ml-3">
        Back to cases
      </Button>

      <div className="mb-7 flex flex-wrap items-start justify-between gap-6">
        <div>
          <h1 className="num text-[24px] font-semibold tracking-[-0.02em]">
            {rupees(detail.amount_paise)}
          </h1>
          <p className="mt-1 text-[13px] text-muted">
            {causeLabel(detail.root_cause)} · {detail.error_reason} · order {detail.order_id}
          </p>
        </div>
        <dl className="grid grid-cols-2 gap-x-8 gap-y-1 text-[13px] sm:grid-cols-4">
          <Fact label="Status" value={<Status value={detail.status} />} />
          <Fact label="Attempts" value={detail.attempt_count} />
          <Fact label="Failed" value={when(detail.failed_at)} />
          <Fact label="Customer" value={detail.customer?.name || '—'} />
        </dl>
      </div>

      <Panel
        title="What happened, and what would have happened"
        note="The same failed payment under both policies."
      >
        <Timeline data={timeline} now={clock?.now} />
      </Panel>

      {detail.rule && (
        <Panel title="The rule that decided this">
          <Box className="px-5 py-4">
            <div className="log mb-2 text-muted">{detail.rule.rule_id}</div>
            <p className="max-w-3xl">{detail.rule.why}</p>
            <div className="log mt-4 flex flex-wrap gap-x-6 gap-y-1 text-muted">
              <span>channel {detail.rule.channel}</span>
              <span>
                delay{' '}
                {detail.rule.delay_strategy
                  ? detail.rule.delay_strategy
                  : `${detail.rule.delay_minutes} min`}
              </span>
              <span>max messages {detail.rule.max_messages}</span>
              <span>base conversion {detail.rule.base_conversion}</span>
              <span>suggests alternative {String(detail.rule.suggests_alt_method)}</span>
              <span>states the reason {String(detail.rule.mention_reason)}</span>
            </div>
          </Box>
        </Panel>
      )}

      <Panel
        title="Decision records"
        note="Every decision, with the inputs it saw and every gate check it ran."
      >
        <div className="space-y-4">
          {detail.decision_records.map((record) => (
            <Record
              key={record.id}
              record={record}
              action={detail.actions.find((a) => a.id === record.action_id)}
            />
          ))}
          {detail.decision_records.length === 0 && (
            <Box className="px-5 py-4 text-[13px] text-muted">No decisions recorded yet.</Box>
          )}
        </div>
      </Panel>
    </>
  )
}

function Fact({ label, value }) {
  return (
    <div>
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="num">{value}</dd>
    </div>
  )
}

function Record({ record, action }) {
  return (
    <Box className="px-5 py-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <span className="log font-medium">{record.rule_id}</span>
        <span className="log text-muted">{when(record.created_at)}</span>
      </div>

      <p className="mb-3 max-w-3xl">{record.decision}</p>

      {record.why && (
        <p className="mb-4 max-w-3xl border-l-2 border-rule pl-3 text-[13px] text-muted">
          {record.why}
        </p>
      )}

      <div className="log space-y-3">
        <Section label="inputs">
          <div className="flex flex-wrap gap-x-5 gap-y-0.5">
            {Object.entries(record.inputs || {}).map(([k, v]) => (
              <span key={k} className="text-muted">
                {k}=<span className="text-ink">{String(v)}</span>
              </span>
            ))}
          </div>
        </Section>

        <Section label="expected value">
          <span>
            {rupees(record.expected_value_paise)}
            <span className="ml-2 text-muted">
              base conversion × amount, discounted per message already spent
            </span>
          </span>
        </Section>

        {record.gate_checks?.length > 0 && (
          <Section label="gate checks">
            <ul className="space-y-0.5">
              {record.gate_checks.map((check) => (
                <li key={check.name} className="flex flex-wrap items-baseline gap-2">
                  <span
                    className={
                      check.passed
                        ? 'text-recovered'
                        : check.outcome === 'halt'
                          ? 'text-halt'
                          : 'text-blocked'
                    }
                  >
                    {check.passed ? 'pass' : check.outcome}
                  </span>
                  <span>{check.name}</span>
                  <span className="text-muted">{check.detail}</span>
                  {!check.enforced && (
                    <span className="text-atrisk">not enforced by this policy</span>
                  )}
                </li>
              ))}
            </ul>
          </Section>
        )}

        {action?.message_body && (
          <Section label={`message sent · ${action.channel} · ${action.message_source}`}>
            <p className="max-w-3xl whitespace-pre-wrap text-ink">{action.message_body}</p>
          </Section>
        )}

        {action?.blocked_reason && (
          <Section label="not sent">
            <span className="text-blocked">{action.blocked_reason}</span>
          </Section>
        )}

        {record.llm_rationale && (
          <Section label={`llm · ${record.llm_model || 'template'}`}>
            <span className="text-muted">{record.llm_rationale}</span>
          </Section>
        )}

        {action?.idempotency_key && (
          <Section label="idempotency key">
            <span className="text-muted">{action.idempotency_key}</span>
          </Section>
        )}
      </div>
    </Box>
  )
}

function Section({ label, children }) {
  return (
    <div className="border-t border-rule pt-2.5">
      <div className="mb-1 text-muted">{label}</div>
      {children}
    </div>
  )
}
