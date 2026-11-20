<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 覆盖比, 覆盖架构, 覆盖状态, 预检状态, 标 } from '../labels'

const periodId = ref('2026-10')
const policy = ref(null)
const scan = ref(null)
const prechecks = ref([])
const snapshot = ref(null)
const msg = ref('')
const err = ref('')

async function load() {
  err.value = ''
  try {
    policy.value = await api.coveragePolicy()
    scan.value = await api.coverageScan(periodId.value)
    prechecks.value = await api.prechecks(periodId.value)
    snapshot.value = await api.coverageSnapshot(periodId.value)
  } catch (e) { err.value = e.message }
}

async function runScan(persist) {
  err.value = ''; msg.value = ''
  try {
    if (persist) {
      const pc = await api.runPrecheck(periodId.value)
      msg.value = `预检 ${pc.precheck_id}：${标(覆盖状态, pc.verdict)}（${pc.covered_key_count}/${pc.estimate_key_count}，缺 ${pc.missing_meters.length} 户）`
      await load()
    } else {
      scan.value = await api.coverageScan(periodId.value, false)
      msg.value = '已扫描（未物化为预检批）'
    }
  } catch (e) { err.value = e.message }
}

onMounted(load)
</script>
<template>
  <div>
    <h1>覆盖率预检</h1>
    <p class="sub">实抄表键数 / 估抄表键数；扫描 → 预检批 → 封印闸 → 快照同一口径</p>
    <div class="row">
      <label>账期<input v-model="periodId" @change="load" /></label>
      <button class="secondary" @click="runScan(false)">仅扫描</button>
      <button @click="runScan(true)">扫描并生成预检批</button>
      <button class="secondary" @click="load">刷新</button>
    </div>
    <p class="msg ok" v-if="msg">{{ msg }}</p>
    <p class="msg err" v-if="err">{{ err }}</p>

    <div class="grid" v-if="scan">
      <div class="stat">口径<b>{{ 标(覆盖架构, policy?.architecture) }}</b></div>
      <div class="stat">阈值<b>{{ 覆盖比(policy?.threshold) }}</b></div>
      <div class="stat">估抄表键<b>{{ scan.estimate_key_count }}</b></div>
      <div class="stat">已覆盖表键<b>{{ scan.covered_key_count }}</b></div>
      <div class="stat">覆盖率<b>{{ 覆盖比(scan.coverage_ratio) }}</b></div>
      <div class="stat">判定<b>{{ 标(覆盖状态, scan.verdict) }}</b></div>
      <div class="stat">缺失表<b>{{ scan.missing_count }}</b></div>
      <div class="stat">预检状态<b>{{ scan.active_precheck ? 标(预检状态, scan.active_precheck.status) : '无' }}</b></div>
    </div>
    <p class="msg" v-if="scan && scan.active_precheck_stale === true">
      数据或口径已变动，当前有效预检已过期，封印前需重新扫描。
    </p>

    <div class="panel" v-if="scan">
      <h3>缺失表（应抄未抄）</h3>
      <p v-if="!scan.missing_meters.length" class="msg ok">无缺失</p>
      <table v-else>
        <thead><tr><th>#</th><th>表号</th></tr></thead>
        <tbody>
          <tr v-for="(m, i) in scan.missing_meters" :key="m">
            <td>{{ i + 1 }}</td><td>{{ m }}</td>
          </tr>
        </tbody>
      </table>
      <router-link to="/coverage/missing">查看缺失表清单页 →</router-link>
    </div>

    <div class="panel">
      <h3>预检批</h3>
      <table>
        <thead><tr><th>预检号</th><th>架构</th><th>覆盖</th><th>阈值</th><th>缺失</th><th>判定</th><th>状态</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="p in prechecks" :key="p.precheck_id">
            <td>{{ p.precheck_id }}</td>
            <td>{{ 标(覆盖架构, p.architecture) }}</td>
            <td>{{ p.covered_key_count }}/{{ p.estimate_key_count }}（{{ 覆盖比(p.coverage_ratio) }}）</td>
            <td>{{ 覆盖比(p.threshold) }}</td>
            <td>{{ p.missing_meters.join('、') || '—' }}</td>
            <td>{{ 标(覆盖状态, p.verdict) }}</td>
            <td>{{ 标(预检状态, p.status) }}</td>
            <td>{{ p.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="snapshot">
      <h3>封印快照{{ snapshot.sealed ? '（已落封）' : '（按当前口径预览）' }}</h3>
      <table>
        <tbody>
          <tr><th>口径</th><td>{{ 标(覆盖架构, snapshot.architecture) }}</td></tr>
          <tr><th>状态</th><td>{{ 标(覆盖状态, snapshot.state) }}</td></tr>
          <tr><th>覆盖率</th><td>{{ 覆盖比(snapshot.coverage_ratio) }}</td></tr>
          <tr><th>阈值</th><td>{{ 覆盖比(snapshot.threshold) }}</td></tr>
          <tr><th>缺失表</th><td>{{ snapshot.missing_meters.join('、') || '—' }}</td></tr>
          <tr><th>绑定预检</th><td>{{ snapshot.precheck_id || '—' }}</td></tr>
          <tr><th>数据指纹</th><td>{{ snapshot.data_fingerprint || '—' }}</td></tr>
          <tr v-if="!snapshot.sealed"><th>可封</th><td>{{ snapshot.gate_ok ? '是' : '否：' + (snapshot.gate_reason || '') }}</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
