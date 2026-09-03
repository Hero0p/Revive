import { rupeesShort } from '../api'

export function Panel({ title, note, children, right }) {
  return (
    <section className="mb-10">
      {(title || right) && (
        <div className="mb-3 flex items-baseline justify-between gap-4">
          <div>
            {title && <h2 className="text-[15px] font-semibold tracking-[-0.01em]">{title}</h2>}
            {note && <p className="mt-0.5 text-[13px] text-muted">{note}</p>}
          </div>
          {right}
        </div>
      )}
      {children}
    </section>
  )
}

export function Box({ children, className = '' }) {
  return (
    <div className={`border border-rule bg-white rounded-[5px] ${className}`}>{children}</div>
  )
}

/** A headline number with the baseline it is being compared against. */
export function Metric({ label, value, baseline, tone = 'ink', hint, better }) {
  const tones = {
    ink: 'text-ink',
    recovered: 'text-recovered',
    atrisk: 'text-atrisk',
    blocked: 'text-blocked',
    halt: 'text-halt',
  }
  return (
    <div className="border-l border-rule pl-4 first:border-l-0 first:pl-0">
      <div className="text-[13px] text-muted">{label}</div>
      <div className={`num mt-1 text-[28px] font-semibold tracking-[-0.02em] ${tones[tone]}`}>
        {value}
      </div>
      {baseline !== undefined && baseline !== null && (
        <div className="num mt-1 text-[13px] text-muted">
          {baseline} with the baseline
          {better && <span className="ml-1.5 text-recovered">{better}</span>}
        </div>
      )}
      {hint && <div className="mt-1 text-[12px] text-muted">{hint}</div>}
    </div>
  )
}

export function Money({ paise, tone = 'ink', className = '' }) {
  const tones = { ink: '', recovered: 'text-recovered', atrisk: 'text-atrisk' }
  return <span className={`num ${tones[tone]} ${className}`}>{rupeesShort(paise)}</span>
}

const STATUS_STYLE = {
  recovered: 'text-recovered',
  detected: 'text-ink',
  planned: 'text-ink',
  acting: 'text-atrisk',
  exhausted: 'text-muted',
  suppressed: 'text-blocked',
  escalated: 'text-halt',
  sent: 'text-ink',
  pending: 'text-atrisk',
  blocked: 'text-blocked',
  failed: 'text-halt',
}

export function Status({ value }) {
  return <span className={STATUS_STYLE[value] || 'text-muted'}>{value}</span>
}

export function Empty({ title, action, children }) {
  return (
    <Box className="px-6 py-10 text-center">
      <p className="text-[15px]">{title}</p>
      {children && <p className="mt-1 text-[13px] text-muted">{children}</p>}
      {action && <div className="mt-4">{action}</div>}
    </Box>
  )
}

export function Button({ children, onClick, kind = 'default', disabled, className = '' }) {
  const kinds = {
    default: 'border-rule bg-white hover:bg-[#f4f4f0]',
    primary: 'border-ink bg-ink text-white hover:bg-[#000]',
    quiet: 'border-transparent bg-transparent text-muted hover:text-ink',
  }
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`rounded-[4px] border px-3 py-1.5 text-[13px] transition-colors disabled:opacity-40 ${kinds[kind]} ${className}`}
    >
      {children}
    </button>
  )
}

export function Field({ label, children }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[13px] text-muted">{label}</span>
      {children}
    </label>
  )
}

export const inputClass =
  'w-full rounded-[4px] border border-rule bg-white px-2.5 py-1.5 outline-none focus:border-ink'

export function Th({ children, right }) {
  return (
    <th
      className={`border-b border-rule pb-2 pr-4 text-[12px] font-medium text-muted ${
        right ? 'text-right' : 'text-left'
      }`}
    >
      {children}
    </th>
  )
}

export function Td({ children, right, className = '' }) {
  return (
    <td
      className={`border-b border-rule py-2.5 pr-4 align-top ${
        right ? 'num text-right' : ''
      } ${className}`}
    >
      {children}
    </td>
  )
}

export function Spinner({ label = 'Loading' }) {
  return <p className="py-8 text-[13px] text-muted">{label}…</p>
}
