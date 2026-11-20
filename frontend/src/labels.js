/** 界面文案映射：库内码 → 中文（页面不直接露英文码） */
export const 表类型 = { water: '水表', electric: '电表', 水表: '水表', 电表: '电表' }
export const 表状态 = { active: '在用', inactive: '停用', 在用: '在用', 停用: '停用' }
export const 账期状态 = { open: '开放', sealed: '已封印', expired: '已作废', 开放: '开放', 已封印: '已封印' }
export const 批状态 = { open: '开放', sealed: '已封印', expired: '已作废' }
export const 命运 = {
  pending: '未决',
  accept_short: '认短',
  accept_over: '认溢',
  quarantine: '隔离',
}

export const 覆盖架构 = {
  hard_gate: '覆盖率硬闸',
  allow_skip: '允许低覆盖直封',
}
export const 覆盖状态 = {
  pass: '达标',
  fail: '不足',
  skipped: '未强制',
}
export const 预检状态 = {
  active: '有效',
  superseded: '已作废',
  sealed: '随封印',
}
export function 覆盖比(v) {
  if (v == null) return '未计算'
  return (v * 100).toFixed(1) + '%'
}
export function 标(map, key) {
  if (key == null || key === '') return '—'
  return map[key] || String(key)
}
