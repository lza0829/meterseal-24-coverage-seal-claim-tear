<script setup>
import { onMounted, ref, watch } from 'vue'
import { api } from '../api'
import { 表类型, 表状态, 覆盖比, 覆盖架构, 标 } from '../labels'

const periodId = ref('2026-10')
const data = ref(null)
const err = ref('')

async function load() {
  err.value = ''
  try { data.value = await api.missing(periodId.value) } catch (e) { err.value = e.message }
}
watch(periodId, load)
onMounted(load)
</script>
<template>
  <div>
    <h1>缺失表清单</h1>
    <p class="sub">有估抄、无实抄的表；与覆盖率扫描、封印快照同一口径</p>
    <div class="row">
      <label>账期<input v-model="periodId" /></label>
      <button class="secondary" @click="load">刷新</button>
    </div>
    <p class="msg err" v-if="err">{{ err }}</p>
    <template v-if="data">
      <div class="grid">
        <div class="stat">口径<b>{{ 标(覆盖架构, data.architecture) }}</b></div>
        <div class="stat">估抄表键<b>{{ data.estimate_key_count }}</b></div>
        <div class="stat">已覆盖<b>{{ data.covered_key_count }}</b></div>
        <div class="stat">覆盖率<b>{{ 覆盖比(data.coverage_ratio) }}</b></div>
        <div class="stat">缺失<b>{{ data.missing_count }}</b></div>
        <div class="stat">未抄（正式键重放）<b>{{ (data.replay_missing_meters || []).length }}</b></div>
      </div>
      <p class="msg" v-if="data.period_status === 'sealed'">该账期已封印，以下为封印时留存的清单。</p>
      <p class="msg" v-if="data.active_precheck_stale === true">当前预检已过期，请重新扫描后再封印。</p>
      <p class="msg err" v-if="data.claim_tear">
        封印快照的未抄清单与正式实抄键集合重放不一致（数据指纹 {{ data.data_fingerprint }} vs
        {{ data.replay_fingerprint }}）。以正式键为准，需重新核对，不得据此认为已抄齐。
      </p>
      <div class="panel">
        <table>
          <thead><tr><th>#</th><th>表号</th><th>楼栋</th><th>户号</th><th>类型</th><th>表状态</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in data.rows" :key="r.meter_id">
              <td>{{ i + 1 }}</td>
              <td>{{ r.meter_id }}</td>
              <td>{{ r.building || '—' }}</td>
              <td>{{ r.unit_label || '—' }}</td>
              <td>{{ 标(表类型, r.meter_type) }}</td>
              <td>{{ 标(表状态, r.status) }}</td>
            </tr>
            <tr v-if="!data.rows.length">
              <td colspan="6">无缺失表</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>
