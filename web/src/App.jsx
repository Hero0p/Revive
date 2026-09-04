import { useCallback, useEffect, useState } from 'react'
import { api, when } from './api'
import { Button } from './components/ui'
import Overview from './screens/Overview'
import Cases from './screens/Cases'
import CaseDetail from './screens/CaseDetail'
import Checkout from './screens/Checkout'
import Simulate from './screens/Simulate'
import Outbox from './screens/Outbox'
import Audit from './screens/Audit'
import Compare from './screens/Compare'
import Rules from './screens/Rules'

const NAV = [
  { id: 'overview', label: 'Overview' },
  { id: 'cases', label: 'Cases' },
  { id: 'checkout', label: 'Checkout' },
  { id: 'simulate', label: 'Simulate' },
  { id: 'outbox', label: 'Outbox' },
  { id: 'audit', label: 'Audit log' },
  { id: 'compare', label: 'Compare' },
  { id: 'rules', label: 'Decision table' },
]

export default function App() {
  const [screen, setScreen] = useState('overview')
  const [caseId, setCaseId] = useState(null)
  const [clock, setClock] = useState(null)
  const [health, setHealth] = useState(null)
  const [tick, setTick] = useState(0)

  const refresh = useCallback(() => setTick((t) => t + 1), [])

  const loadClock = useCallback(async () => {
    try {
      setClock(await api.clock())
      setHealth(await api.health())
    } catch {
      setHealth(null)
    }
  }, [])

  useEffect(() => {
    loadClock()
  }, [loadClock, tick])

  const openCase = (id) => {
    setCaseId(id)
    setScreen('case')
  }

  const go = (id) => {
    setCaseId(null)
    setScreen(id)
  }

  return (
    <div className="flex min-h-full">
      <aside className="w-[196px] shrink-0 border-r border-rule px-5 py-6">
        <div className="mb-8">
          <div className="text-[15px] font-semibold tracking-[-0.01em]">Revive</div>
          <div className="mt-0.5 text-[12px] text-muted">Cause-aware payment recovery</div>
        </div>

        <nav className="space-y-0.5">
          {NAV.map((item) => (
            <button
              key={item.id}
              onClick={() => go(item.id)}
              className={`block w-full rounded-[4px] px-2 py-1.5 text-left text-[13px] transition-colors ${
                screen === item.id || (screen === 'case' && item.id === 'cases')
                  ? 'bg-[#efeee8] font-medium'
                  : 'text-muted hover:text-ink'
              }`}
            >
              {item.label}
            </button>
          ))}
        </nav>

        {health && <Connection health={health} />}
      </aside>

      <div className="min-w-0 grow">
        <ClockBar clock={clock} onChange={refresh} />
        <main className="px-8 py-7">
          {screen === 'overview' && <Overview tick={tick} onOpenCase={openCase} go={go} />}
          {screen === 'cases' && <Cases tick={tick} onOpenCase={openCase} />}
          {screen === 'case' && (
            <CaseDetail caseId={caseId} tick={tick} clock={clock} onBack={() => go('cases')} />
          )}
          {screen === 'checkout' && <Checkout onChange={refresh} onOpenCase={openCase} />}
          {screen === 'simulate' && <Simulate tick={tick} onChange={refresh} onOpenCase={openCase} />}
          {screen === 'outbox' && <Outbox tick={tick} onOpenCase={openCase} />}
          {screen === 'audit' && <Audit tick={tick} onOpenCase={openCase} />}
          {screen === 'compare' && <Compare tick={tick} onChange={refresh} />}
          {screen === 'rules' && <Rules />}
        </main>
      </div>
    </div>
  )
}

function ClockBar({ clock, onChange }) {
  const [busy, setBusy] = useState(false)

  const move = async (body) => {
    setBusy(true)
    try {
      await api.advance(body)
      onChange()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-wrap items-center justify-between gap-4 border-b border-rule px-8 py-3">
      <div className="flex items-baseline gap-3">
        <span className="text-[12px] text-muted">Simulated time</span>
        <span className="num text-[15px] font-medium tracking-[-0.01em]">
          {clock ? clock.display : '—'}
        </span>
        {clock?.frozen && <span className="text-[12px] text-muted">frozen</span>}
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <Button onClick={() => move({ minutes: 5 })} disabled={busy}>
          +5 min
        </Button>
        <Button onClick={() => move({ hours: 1 })} disabled={busy}>
          +1 hour
        </Button>
        <Button onClick={() => move({ days: 1 })} disabled={busy}>
          +1 day
        </Button>
        <Button onClick={() => move({ to_next_action: true })} disabled={busy} kind="primary">
          Jump to next action
        </Button>
        <Button
          kind="quiet"
          disabled={busy}
          onClick={async () => {
            await api.resetClock()
            onChange()
          }}
        >
          Reset
        </Button>
      </div>
    </div>
  )
}

function Connection({ health }) {
  const breaker = health.razorpay?.breaker
  return (
    <div className="mt-10 space-y-1 border-t border-rule pt-4 text-[12px] text-muted">
      <div>
        Razorpay{' '}
        <span className={health.razorpay_live ? 'text-recovered' : ''}>
          {health.razorpay_live ? 'live keys' : 'simulated'}
        </span>
      </div>
      <div>
        Circuit breaker{' '}
        <span className={breaker === 'closed' ? '' : 'text-halt'}>{breaker}</span>
      </div>
      <div>
        Message text{' '}
        <span className={health.llm_enabled ? 'text-recovered' : ''}>
          {health.llm_enabled ? health.llm_model : 'templates'}
        </span>
      </div>
      <div className="num">{when(health.clock)}</div>
    </div>
  )
}
