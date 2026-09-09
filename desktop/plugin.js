import {
  Badge, Button, Codicon, host, PALETTE_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS, Switch, Tip, useMutation, useQuery, useQueryClient
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'argos'
let rest

function mask(value) {
  const text = String(value || 'Account')
  const at = text.indexOf('@')
  return at > 0 ? `${text.slice(0, 1)}***${text.slice(at)}` : text
}
function pct(window) {
  return window && typeof window.remaining_pct === 'number' ? Math.round(window.remaining_pct) : null
}
function resetText(epoch) {
  if (!epoch) return 'Reset unknown'
  const seconds = Math.max(0, Math.floor(epoch - Date.now() / 1000))
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return `Resets in ${days ? `${days}d ` : ''}${hours ? `${hours}h ` : ''}${minutes}m`
}
async function status(force = false) {
  return rest(force ? '/status?force=true' : '/status')
}
function Meter({ label, window }) {
  const value = pct(window)
  return jsxs('div', { className: 'space-y-1.5', children: [
    jsxs('div', { className: 'flex items-center justify-between text-xs', children: [
      jsx('span', { className: 'text-(--ui-text-secondary)', children: label }),
      jsx('strong', { className: 'tabular-nums', children: value == null ? '—' : `${value}%` })
    ] }),
    jsx('div', { className: 'h-1.5 overflow-hidden rounded-full bg-(--ui-fill-secondary)', children:
      jsx('div', { className: 'h-full rounded-full bg-(--ui-accent) transition-[width]', style: { width: `${value || 0}%` } })
    }),
    jsx('div', { className: 'text-[0.68rem] text-(--ui-text-quaternary)', children: resetText(window?.reset_at) })
  ] })
}
function AccountCard({ account, compact = false }) {
  const queryClient = useQueryClient()
  const activate = useMutation({
    mutationFn: () => rest('/use', { method: 'POST', body: { selector: account.id } }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [ID, 'status'] }),
    onError: error => host.notify({ kind: 'error', message: String(error?.message || error) })
  })
  const windows = account.usage?.windows || {}
  return jsxs('section', {
    className: `space-y-4 border p-3 ${account.active ? 'border-(--ui-accent)' : 'border-(--ui-stroke-secondary)'}`,
    children: [
      jsxs('header', { className: 'flex items-start justify-between gap-2', children: [
        jsxs('div', { className: 'min-w-0', children: [
          jsx('h3', { className: 'truncate text-sm font-medium', children: mask(account.label) }),
          jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: account.usage?.plan || 'Plan unavailable' })
        ] }),
        jsxs('div', { className: 'flex gap-1', children: [
          account.active ? jsx(Badge, { children: 'Active' }) : null,
          jsx(Badge, { variant: account.health?.state === 'ok' ? 'secondary' : 'destructive', children: account.health?.state || 'unknown' })
        ] })
      ] }),
      jsx(Meter, { label: '5-hour window', window: windows.five_hour }),
      jsx(Meter, { label: 'Weekly window', window: windows.weekly }),
      !compact && account.usage?.banked_resets?.available_count
        ? jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: `${account.usage.banked_resets.available_count} reset credit(s) banked` }) : null,
      !compact ? jsxs('footer', { className: 'flex items-center justify-between gap-2 border-t border-(--ui-stroke-secondary) pt-3', children: [
        jsx('code', { className: 'text-[0.65rem] text-(--ui-text-quaternary)', children: account.fingerprint }),
        jsx(Button, { size: 'sm', disabled: account.active || activate.isPending, onClick: () => activate.mutate(), children: account.active ? 'In use' : 'Use account' })
      ] }) : null
    ]
  })
}
function Dashboard({ compact = false }) {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: [ID, 'status'], queryFn: () => status(false), refetchInterval: 60_000 })
  const refresh = useMutation({
    mutationFn: () => status(true),
    onSuccess: data => queryClient.setQueryData([ID, 'status'], data),
    onError: error => host.notify({ kind: 'error', message: String(error?.message || error) })
  })
  const toggle = useMutation({
    mutationFn: enabled => rest('/auto', { method: 'POST', body: { enabled } }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [ID, 'status'] })
  })
  const data = query.data
  if (query.isError) return jsx('div', { className: 'p-3 text-sm text-(--ui-danger)', children: String(query.error?.message || query.error) })
  if (!data) return jsx('div', { className: 'p-3 text-sm text-(--ui-text-tertiary)', children: 'Loading Codex subscriptions…' })
  return jsxs('div', { className: compact ? 'h-full space-y-3 overflow-auto p-3' : 'mx-auto max-w-5xl space-y-5 overflow-auto p-5', children: [
    jsxs('header', { className: 'flex items-start justify-between gap-4', children: [
      jsxs('div', { children: [jsx('h2', { className: compact ? 'text-sm font-medium' : 'text-xl font-semibold', children: 'ARGOS' }), !compact ? jsx('p', { className: 'text-sm text-(--ui-text-tertiary)', children: 'Autonomous Rotation & Governance of Subscriptions' }) : null] }),
      jsxs('div', { className: 'flex items-center gap-2', children: [
        jsxs('label', { className: 'flex items-center gap-2 text-xs text-(--ui-text-secondary)', children: [jsx('span', { children: 'Auto' }), jsx(Switch, { checked: data.auto_rotate, onCheckedChange: enabled => toggle.mutate(Boolean(enabled)) })] }),
        jsx(Button, { size: 'sm', variant: 'outline', disabled: refresh.isPending, onClick: () => refresh.mutate(), children: refresh.isPending ? 'Refreshing…' : 'Refresh' })
      ] })
    ] }),
    data.all_unhealthy ? jsx('div', { className: 'border border-(--ui-danger) p-3 text-sm text-(--ui-danger)', children: 'All Codex subscriptions are unavailable. Hermes fallback_model remains authoritative.' }) : null,
    data.accounts.length ? jsx('div', { className: compact ? 'space-y-3' : 'grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-3', children: data.accounts.map(account => jsx(AccountCard, { account, compact }, account.id)) }) :
      jsx('div', { className: 'border border-(--ui-stroke-secondary) p-4 text-sm text-(--ui-text-tertiary)', children: 'No accounts. Run hermes auth add openai-codex for each subscription.' })
  ] })
}
function Chip() {
  const query = useQuery({ queryKey: [ID, 'status'], queryFn: () => status(false), refetchInterval: 60_000 })
  const data = query.data
  if (!data || !data.accounts?.length) return null
  const active = data.accounts.find(account => account.active)
  const tip = data.accounts.map(account => `${mask(account.label)}: ${account.health?.state || 'unknown'} · 5h ${pct(account.usage?.windows?.five_hour) ?? '—'}% · week ${pct(account.usage?.windows?.weekly) ?? '—'}%`).join('\n')
  const remaining = data.active_worst_remaining_pct
  return jsx(Tip, { label: tip, children: jsxs('button', {
    type: 'button', onClick: () => host.navigate('/argos'),
    className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground',
    children: [jsx(Codicon, { name: 'gauge', size: '0.7rem' }), jsx('span', { children: `Codex ${typeof remaining === 'number' ? `${Math.round(remaining)}%` : active?.health?.state || '—'}` })]
  }) })
}

export default {
  id: ID,
  name: 'ARGOS',
  description: 'Autonomous Rotation & Governance of Subscriptions for the OpenAI Codex OAuth pool.',
  defaultEnabled: true,
  register(ctx) {
    rest = ctx.rest
    ctx.registerMany([
      { id: 'pane', area: 'panes', title: 'ARGOS', data: { placement: 'right', width: '320px' }, render: () => jsx(Dashboard, { compact: true }) },
      { id: 'page', area: ROUTES_AREA, data: { path: '/argos' }, render: () => jsx(Dashboard, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 55, data: { path: '/argos', label: 'ARGOS', codicon: 'gauge' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 82, render: () => jsx(Chip, {}) },
      { id: 'open', area: PALETTE_AREA, data: { id: 'argos.open', label: 'ARGOS: Open dashboard', keywords: ['argos', 'codex', 'quota', 'subscription'], run: () => host.navigate('/argos') } }
    ])
  }
}
