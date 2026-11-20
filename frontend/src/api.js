const base = '/api'

async function req(path, opts = {}) {
  const res = await fetch(base + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  const text = await res.text()
  let data
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!res.ok) {
    const msg = (data && data.detail) || text || res.statusText
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return data
}

export const api = {
  overview: () => req('/overview'),
  meters: () => req('/meters'),
  estimates: (periodId) => req('/estimates' + (periodId ? `?period_id=${periodId}` : '')),
  actuals: (periodId) => req('/actuals' + (periodId ? `?period_id=${periodId}` : '')),
  postEstimate: (body) => req('/estimates', { method: 'POST', body: JSON.stringify(body) }),
  postActual: (body) => req('/actuals', { method: 'POST', body: JSON.stringify(body) }),
  periods: () => req('/periods'),
  align: (period_id) => req('/align', { method: 'POST', body: JSON.stringify({ period_id }) }),
  batches: (periodId) => req('/batches' + (periodId ? `?period_id=${periodId}` : '')),
  variances: (q) => {
    const sp = new URLSearchParams(q || {})
    const s = sp.toString()
    return req('/variances' + (s ? `?${s}` : ''))
  },
  setFate: (id, fate) => req(`/variances/${id}/fate`, { method: 'POST', body: JSON.stringify({ fate }) }),
  seal: (body) => req('/seal', { method: 'POST', body: JSON.stringify(body) }),
  settings: () => req('/settings'),
  coveragePolicy: () => req('/coverage/policy'),
  putCoveragePolicy: (body) => req('/coverage/policy', { method: 'PUT', body: JSON.stringify(body) }),
  coverageScan: (periodId, persist = false) =>
    req(`/coverage/scan?period_id=${encodeURIComponent(periodId)}&persist=${persist}`),
  runPrecheck: (periodId) => req(`/coverage/precheck?period_id=${encodeURIComponent(periodId)}`, { method: 'POST' }),
  prechecks: (periodId) => req('/coverage/prechecks' + (periodId ? `?period_id=${encodeURIComponent(periodId)}` : '')),
  missing: (periodId) => req(`/coverage/missing?period_id=${encodeURIComponent(periodId)}`),
  coverageSnapshot: (periodId) => req(`/coverage/snapshot?period_id=${encodeURIComponent(periodId)}`),
  coverageGate: (periodId) => req(`/coverage/gate?period_id=${encodeURIComponent(periodId)}`),
  putSetting: (key, value) => req(`/settings/${key}`, { method: 'PUT', body: JSON.stringify({ value }) }),
}
