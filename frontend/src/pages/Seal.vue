<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 账期状态, 覆盖架构, 覆盖状态, 覆盖比, 标 } from '../labels'
const periods = ref([])
const periodId = ref('2026-10')
const batchId = ref('')
const gate = ref(null)
const msg = ref('')
const err = ref('')
async function refresh() {
  periods.value = await api.periods()
  const bs = await api.batches(periodId.value)
  batchId.value = bs.find(b => b.status === 'open')?.batch_id || bs[0]?.batch_id || ''
  try { gate.value = await api.coverageGate(periodId.value) } catch { gate.value = null }
}
async function seal() {
  err.value = ''; msg.value = ''
  try {
    const r = await api.seal({ period_id: periodId.value, batch_id: batchId.value || null })
    const cov = r.coverage || {}
    msg.value = `已封印 ${r.period_id}，绑定批 ${r.batch_id}；覆盖率 ${覆盖比(cov.coverage_ratio)}（${标(覆盖状态, cov.state)}）`
    await refresh()
  } catch (e) { err.value = e.message; await refresh() }
}
onMounted(refresh)
</script>
<template>
  <div>
    <h1>账期封印</h1>
    <p class="sub">封印后该期估抄、实抄禁止再写；封印前按当前覆盖率口径判闸</p>
    <div class="row">
      <label>账期<input v-model="periodId" @change="refresh" /></label>
      <label>对账批<input v-model="batchId" placeholder="可空取最新开放批" /></label>
      <button class="warn" @click="seal">封印账期</button>
      <button class="secondary" @click="refresh">刷新</button>
    </div>
    <p class="msg ok" v-if="msg">{{ msg }}</p>
    <p class="msg err" v-if="err">{{ err }}</p>

    <div class="panel" v-if="gate">
      <h3>封印闸</h3>
      <div class="grid">
        <div class="stat">口径<b>{{ 标(覆盖架构, gate.policy.architecture) }}</b></div>
        <div class="stat">阈值<b>{{ 覆盖比(gate.policy.threshold) }}</b></div>
        <div class="stat">可封<b>{{ gate.ok ? '是' : '否' }}</b></div>
      </div>
      <p class="msg" v-if="gate.precheck">
        有效预检 {{ gate.precheck.precheck_id }} · 覆盖 {{ gate.precheck.covered_key_count }}/{{ gate.precheck.estimate_key_count }}
        （{{ 覆盖比(gate.precheck.coverage_ratio) }}）· 判定 {{ 标(覆盖状态, gate.precheck.verdict) }}
        <span v-if="gate.stale">· 已过期</span>
      </p>
      <p class="msg err" v-if="!gate.ok">{{ gate.reason }}</p>
      <p class="msg"><router-link to="/coverage">去覆盖率预检</router-link></p>
    </div>

    <div class="panel">
      <table>
        <thead><tr><th>账期</th><th>状态</th><th>封印时刻</th><th>绑定批</th><th>覆盖率快照</th><th>缺失表</th></tr></thead>
        <tbody>
          <tr v-for="p in periods" :key="p.period_id">
            <td>{{ p.period_id }}</td>
            <td><span class="tag" :class="p.status">{{ 标(账期状态, p.status) }}</span></td>
            <td>{{ p.sealed_at || '—' }}</td>
            <td>{{ p.batch_no || '—' }}</td>
            <td>
              <template v-if="p.status === 'sealed'">
                {{ 标(覆盖状态, p.coverage_state) }} · {{ 覆盖比(p.coverage_ratio) }}
              </template>
              <template v-else>—</template>
            </td>
            <td>{{ (p.coverage_missing || '').split(',').filter(Boolean).join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
