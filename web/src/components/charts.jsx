import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { causeLabel, rupeesShort } from '../api'

/**
 * Two series, validated as a categorical pair (CVD ΔE 13.4 deutan / 13.8
 * tritan, normal-vision ΔE 23.9, both above the chroma floor and 3:1 contrast).
 * Baseline gets a non-status identity hue; the router keeps the recovered
 * green, because that series really is the money that came back.
 */
export const SERIES = {
  baseline: '#7A4B9C',
  router: '#0F7B4F',
}

const AXIS = { fill: '#6E7480', fontSize: 12, fontFamily: 'Inter, sans-serif' }
const GRID = '#E4E4DF'

function TooltipBox({ active, payload, label, format }) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-[4px] border border-rule bg-white px-3 py-2 text-[12px]">
      <div className="mb-1 font-medium">{label}</div>
      {payload.map((row) => (
        <div key={row.dataKey} className="flex items-center gap-2">
          <span
            className="h-[7px] w-[7px] shrink-0 rounded-[1px]"
            style={{ background: row.color }}
          />
          <span className="text-muted">{row.name}</span>
          <span className="num ml-auto">{format ? format(row.value) : row.value}</span>
        </div>
      ))}
    </div>
  )
}

/** Recovery rate by failure cause. Horizontal, because cause names are words. */
export function RecoveryByCause({ baseline, router }) {
  const causes = new Map()
  for (const row of baseline || []) {
    causes.set(row.root_cause, { cause: causeLabel(row.root_cause), baseline: row.recovery_rate * 100 })
  }
  for (const row of router || []) {
    const existing = causes.get(row.root_cause) || { cause: causeLabel(row.root_cause) }
    causes.set(row.root_cause, { ...existing, router: row.recovery_rate * 100 })
  }
  const data = [...causes.values()].sort((a, b) => (b.router ?? 0) - (a.router ?? 0))

  return (
    <ResponsiveContainer width="100%" height={Math.max(240, data.length * 42)}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 0, left: 0 }} barGap={2}>
        <CartesianGrid horizontal={false} stroke={GRID} />
        <XAxis
          type="number"
          tick={AXIS}
          tickLine={false}
          axisLine={{ stroke: GRID }}
          unit="%"
        />
        <YAxis
          type="category"
          dataKey="cause"
          tick={AXIS}
          tickLine={false}
          axisLine={{ stroke: GRID }}
          width={130}
        />
        <Tooltip
          cursor={{ fill: '#00000008' }}
          content={<TooltipBox format={(v) => `${(v ?? 0).toFixed(1)}%`} />}
        />
        <Legend
          wrapperStyle={{ fontSize: 12, color: '#6E7480', paddingTop: 8 }}
          iconType="square"
          iconSize={8}
        />
        <Bar dataKey="baseline" name="Baseline" fill={SERIES.baseline} radius={[0, 4, 4, 0]} />
        <Bar dataKey="router" name="Router" fill={SERIES.router} radius={[0, 4, 4, 0]} />
      </BarChart>
    </ResponsiveContainer>
  )
}

/** Messages sent against cases recovered. Both are counts, so one axis. */
export function MessagesVersusRecovered({ policies }) {
  const data = ['baseline', 'router']
    .filter((p) => policies?.[p])
    .map((p) => ({
      policy: p === 'baseline' ? 'Baseline' : 'Router',
      messages: policies[p].messages_sent,
      recovered: policies[p].cases_recovered,
    }))

  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -12 }} barGap={2}>
        <CartesianGrid vertical={false} stroke={GRID} />
        <XAxis dataKey="policy" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} />
        <Tooltip cursor={{ fill: '#00000008' }} content={<TooltipBox />} />
        <Legend
          wrapperStyle={{ fontSize: 12, color: '#6E7480', paddingTop: 8 }}
          iconType="square"
          iconSize={8}
        />
        <Bar dataKey="messages" name="Messages sent" fill={SERIES.baseline} radius={[4, 4, 0, 0]} />
        <Bar
          dataKey="recovered"
          name="Cases recovered"
          fill={SERIES.router}
          radius={[4, 4, 0, 0]}
        />
      </BarChart>
    </ResponsiveContainer>
  )
}

/** Money at risk against money recovered, per cause. Used on Compare. */
export function MoneyByCause({ causes }) {
  const data = (causes || []).map((row) => ({
    cause: causeLabel(row.root_cause),
    at_risk: row.at_risk_paise / 100,
    recovered: row.recovered_paise / 100,
  }))

  return (
    <ResponsiveContainer width="100%" height={Math.max(240, data.length * 42)}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 0, left: 0 }} barGap={2}>
        <CartesianGrid horizontal={false} stroke={GRID} />
        <XAxis
          type="number"
          tick={AXIS}
          tickLine={false}
          axisLine={{ stroke: GRID }}
          tickFormatter={(v) => rupeesShort(v * 100)}
        />
        <YAxis
          type="category"
          dataKey="cause"
          tick={AXIS}
          tickLine={false}
          axisLine={{ stroke: GRID }}
          width={130}
        />
        <Tooltip
          cursor={{ fill: '#00000008' }}
          content={<TooltipBox format={(v) => rupeesShort(v * 100)} />}
        />
        <Legend
          wrapperStyle={{ fontSize: 12, color: '#6E7480', paddingTop: 8 }}
          iconType="square"
          iconSize={8}
        />
        <Bar dataKey="at_risk" name="At risk" fill={SERIES.baseline} radius={[0, 4, 4, 0]} />
        <Bar dataKey="recovered" name="Recovered" fill={SERIES.router} radius={[0, 4, 4, 0]} />
      </BarChart>
    </ResponsiveContainer>
  )
}
