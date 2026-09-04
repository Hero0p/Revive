async function req(url, options) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = (await response.json()).detail || detail
    } catch {
      /* keep the status text */
    }
    throw new Error(detail)
  }
  return response.json()
}

const post = (url, body) => req(url, { method: 'POST', body: JSON.stringify(body ?? {}) })

export const api = {
  health: () => req('/api/health'),
  rules: () => req('/api/rules'),

  cases: (params = {}) => req('/api/cases?' + new URLSearchParams(params)),
  caseDetail: (id) => req(`/api/cases/${id}`),
  timeline: (id) => req(`/api/cases/${id}/timeline`),
  outbox: (params = {}) => req('/api/outbox?' + new URLSearchParams(params)),
  audit: (params = {}) => req('/api/audit?' + new URLSearchParams(params)),
  events: () => req('/api/events'),

  clock: () => req('/api/clock'),
  advance: (body) => post('/api/clock/advance', body),
  resetClock: () => post('/api/clock/reset'),

  chaos: () => req('/api/chaos'),
  setChaos: (body) => post('/api/chaos', body),

  createOrder: (body) => post('/api/orders', body),
  verifyPayment: (body) => post('/api/verify-payment', body),
  syncRazorpay: () => post('/api/razorpay/sync'),
  reclassify: (caseId, body) => post(`/api/cases/${caseId}/reclassify`, body),

  inject: (body) => post('/api/sim/inject', body),
  capture: (body) => post('/api/sim/capture', body),
  tamper: () => post('/api/sim/tamper'),

  runs: () => req('/api/runs'),
  createRun: (body) => post('/api/runs', body),
  runStatus: () => req('/api/runs/status'),
  byCause: (id) => req(`/api/runs/${id}/by-cause`),
  compare: (seed, count) => req(`/api/runs/compare/${seed}/${count}`),
  sweep: (body) => post('/api/runs/sweep', body),
}

/** How many synthetic failures the dashboard compares over.
 *
 * 3,000 is the number every published result uses. It is overridable at build
 * time (VITE_COMPARISON_COUNT) only because a small free hosting instance can
 * take a long while to chew through that many, and a deployed demo that never
 * finishes its first run is worse than one that compares fewer cases. */
export const COMPARISON_COUNT = Number(import.meta.env.VITE_COMPARISON_COUNT) || 3000
export const COMPARISON_SEED = Number(import.meta.env.VITE_COMPARISON_SEED) || 42

/** Waits for the background comparison to finish, reporting each poll.
 *
 * A run takes minutes, which is longer than a browser or a hosting proxy will
 * hold a request open, so the server starts it and we poll instead. A failed
 * poll is ignored rather than fatal: an instance that has just woken up drops
 * the occasional request, and the job is still running regardless. */
export async function awaitJob(onProgress, { intervalMs = 2000 } = {}) {
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs))
    let status
    try {
      status = await api.runStatus()
    } catch {
      continue
    }
    onProgress?.(status)
    if (status.state !== 'running') return status
  }
}

/** "router (2 of 2) · 41s" for the progress line under a running job. */
export function jobProgress(job) {
  if (!job) return ''
  const step = job.step ? policyLabel(job.step.split(' ')[0]) + job.step.slice(job.step.indexOf(' ')) : ''
  const secs = job.elapsed_seconds != null ? `${Math.round(job.elapsed_seconds)}s` : ''
  return [step, secs].filter(Boolean).join(' · ')
}

/** 400000 -> "₹4,000", with Indian digit grouping. */
export function rupees(paise, { decimals = 0 } = {}) {
  const value = (paise ?? 0) / 100
  return (
    '₹' +
    value.toLocaleString('en-IN', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })
  )
}

/** Compact for headline metrics: ₹5.3L, ₹1.2Cr. */
export function rupeesShort(paise) {
  const value = (paise ?? 0) / 100
  if (value >= 1e7) return '₹' + (value / 1e7).toFixed(2) + 'Cr'
  if (value >= 1e5) return '₹' + (value / 1e5).toFixed(2) + 'L'
  if (value >= 1000) return '₹' + (value / 1000).toFixed(1) + 'k'
  return rupees(paise)
}

export function when(iso, { withDate = true } = {}) {
  if (!iso) return '—'
  const d = new Date(iso)
  const time = d.toLocaleTimeString('en-IN', {
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  })
  if (!withDate) return time
  return `${d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })}, ${time}`
}

export function gapFrom(startIso, endIso) {
  if (!startIso || !endIso) return ''
  const minutes = Math.round((new Date(endIso) - new Date(startIso)) / 60000)
  if (minutes < 1) return 'immediately'
  if (minutes < 60) return `+${minutes} min`
  if (minutes < 60 * 24) return `+${Math.round(minutes / 60)} h`
  return `+${Math.round(minutes / 1440)} d`
}

export const CAUSE_LABEL = {
  transient_network: 'Network timeout',
  data_entry: 'Card typo',
  otp_failure: 'OTP failure',
  gateway_degraded: 'Gateway outage',
  balance: 'Insufficient funds',
  card_config: 'Card blocked online',
  issuer_decline: 'Bank declined',
  deliberate_abandon: 'Abandoned on purpose',
  unknown: 'Unmapped',
}

export const causeLabel = (c) => CAUSE_LABEL[c] || c || '—'

/** The cause-aware policy is the product, and it is called Revive.
 *
 * The stored value stays "router": it is the API contract, the `policy` column
 * on every case, and the prefix of every run id. Only the label changes. */
export const policyLabel = (p) =>
  ({ router: 'Revive', baseline: 'Baseline' }[p] || p || '—')

/** "router-s42-n3000" -> "Revive · seed 42 · 3,000 cases". Falls back to the
 *  raw id for anything that does not match the shape. */
export function runLabel(runId) {
  const parts = /^([a-z]+)-s(\d+)-n(\d+)$/.exec(runId || '')
  if (!parts) return runId
  const [, policy, seed, count] = parts
  return `${policyLabel(policy)} · seed ${seed} · ${Number(count).toLocaleString('en-IN')} cases`
}
