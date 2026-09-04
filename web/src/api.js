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
  byCause: (id) => req(`/api/runs/${id}/by-cause`),
  compare: (seed, count) => req(`/api/runs/compare/${seed}/${count}`),
  sweep: (body) => post('/api/runs/sweep', body),
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
