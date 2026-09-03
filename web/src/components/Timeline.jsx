import { gapFrom, when } from '../api'

/**
 * The signature element: one failed payment, two lanes.
 *
 * The reader should understand the whole product from this graphic without a
 * word of explanation -- the baseline sends at the same moment every time, the
 * router waits for the moment that can actually work.
 *
 * Time is on a square-root scale. A linear axis would squash a 2-minute
 * response and a 3-week payday wait into the same pixel.
 */

const LANE_ORDER = ['baseline', 'router']

export default function Timeline({ data, now }) {
  if (!data || !data.lanes) return null
  const lanes = LANE_ORDER.filter((p) => data.lanes[p]).map((p) => data.lanes[p])
  const extra = Object.values(data.lanes).filter((l) => !LANE_ORDER.includes(l.policy))
  const shown = lanes.length ? lanes : extra
  if (!shown.length) return null

  const origin = new Date(data.origin || shown[0].failed_at)
  const times = shown.flatMap((lane) => lane.events.map((e) => new Date(e.at)))
  if (now) times.push(new Date(now))
  const maxHours = Math.max(
    1,
    ...times.map((t) => (t - origin) / 3600000).filter((h) => Number.isFinite(h)),
  )

  const at = (iso) => {
    const hours = Math.max(0, (new Date(iso) - origin) / 3600000)
    return (Math.sqrt(hours) / Math.sqrt(maxHours)) * 96 + 2
  }

  return (
    <div className="border border-rule bg-white rounded-[5px] px-6 pb-4 pt-5">
      <Axis maxHours={maxHours} at={at} origin={origin} />
      {shown.map((lane) => (
        <Lane key={lane.policy + lane.case_id} lane={lane} at={at} now={now} origin={origin} />
      ))}
      <Legend />
    </div>
  )
}

function Axis({ maxHours, at, origin }) {
  const candidates = [
    { hours: 0, label: 'T+0' },
    { hours: 1 / 12, label: '5 min' },
    { hours: 1, label: '1 hour' },
    { hours: 6, label: '6 hours' },
    { hours: 24, label: 'Day 1' },
    { hours: 24 * 3, label: 'Day 3' },
    { hours: 24 * 7, label: 'Day 7' },
    { hours: 24 * 14, label: 'Day 14' },
    { hours: 24 * 30, label: 'Day 30' },
  ].filter((t) => t.hours <= maxHours * 1.02)

  return (
    <div className="relative mb-1 h-4">
      {candidates.map((tick) => (
        <span
          key={tick.label}
          className="absolute -translate-x-1/2 text-[11px] text-muted whitespace-nowrap"
          style={{ left: `${at(new Date(origin.getTime() + tick.hours * 3600000))}%` }}
        >
          {tick.label}
        </span>
      ))}
    </div>
  )
}

function Lane({ lane, at, now, origin }) {
  const recovered = lane.status === 'recovered'
  const label = lane.policy === 'baseline' ? 'Baseline' : 'Router'

  return (
    <div className="mb-2 border-t border-rule pt-4 first:border-t-0">
      <div className="mb-6 flex items-start gap-5">
        <div className="w-20 shrink-0 pt-1">
          <div className="text-[13px] font-medium">{label}</div>
          <div className="text-[12px] text-muted">
            {recovered ? 'recovered' : lane.status}
          </div>
        </div>

        <div className="relative min-h-[74px] grow">
          {/* the track */}
          <div className="absolute left-0 right-0 top-[7px] h-px bg-rule" />

          {now && (
            <div
              className="absolute top-0 h-4 w-px bg-atrisk/50 transition-[left] duration-700 ease-out"
              style={{ left: `${at(now)}%` }}
              title={`Now: ${when(now)}`}
            />
          )}

          {lane.events.map((event, i) => (
            <Marker
              key={i}
              event={event}
              left={at(event.at)}
              gap={gapFrom(lane.failed_at, event.at)}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function Marker({ event, left, gap }) {
  const style = {
    failed: { shape: 'dot', color: 'bg-ink', text: 'text-ink' },
    message: { shape: 'diamond', color: 'bg-ink', text: 'text-ink' },
    recovered: { shape: 'dot', color: 'bg-recovered', text: 'text-recovered' },
    blocked: { shape: 'ring', color: 'border-blocked', text: 'text-blocked' },
    pending: { shape: 'ring', color: 'border-atrisk', text: 'text-atrisk' },
    send_failed: { shape: 'ring', color: 'border-halt', text: 'text-halt' },
  }[event.kind] || { shape: 'ring', color: 'border-blocked', text: 'text-blocked' }

  const annotation = annotate(event)

  return (
    <div
      className="absolute top-0 w-[132px] -translate-x-1/2 text-center transition-[left] duration-700 ease-out"
      style={{ left: `${left}%` }}
    >
      <div className="flex h-4 items-center justify-center">
        {style.shape === 'diamond' ? (
          <span className={`block h-[9px] w-[9px] rotate-45 ${style.color}`} />
        ) : style.shape === 'ring' ? (
          <span className={`block h-[9px] w-[9px] rounded-full border-2 bg-white ${style.color}`} />
        ) : (
          <span className={`block h-[9px] w-[9px] rounded-full ${style.color}`} />
        )}
      </div>
      <div className={`mt-1.5 text-[12px] leading-tight ${style.text}`}>{event.label}</div>
      <div className="text-[11px] leading-tight text-muted">{gap}</div>
      {annotation && (
        <div className="mt-0.5 text-[11px] leading-tight text-muted">{annotation}</div>
      )}
    </div>
  )
}

/** The short human reason under each action -- the part that explains itself. */
function annotate(event) {
  if (event.kind === 'blocked') {
    if ((event.note || '').includes('already succeeded')) return 'already paid'
    if ((event.note || '').includes('opted out')) return 'opted out'
    if ((event.note || '').includes('message cap')) return 'message cap'
    if ((event.note || '').includes('superseded')) return 'replaced'
    return 'stopped by the gate'
  }
  if (event.kind === 'message' && event.suggests_alt_method) return 'offered another method'
  if (event.kind === 'recovered') return event.detail
  return null
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-x-5 gap-y-1 border-t border-rule pt-3 text-[11px] text-muted">
      <span className="flex items-center gap-1.5">
        <span className="h-[7px] w-[7px] rounded-full bg-ink" /> failed
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-[7px] w-[7px] rotate-45 bg-ink" /> message sent
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-[7px] w-[7px] rounded-full border-2 border-blocked bg-white" /> stopped
        by the gate
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-[7px] w-[7px] rounded-full bg-recovered" /> paid
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-3 w-px bg-atrisk/60" /> now
      </span>
    </div>
  )
}
