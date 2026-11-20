<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 批状态, 覆盖架构, 覆盖状态, 覆盖比, 标 } from '../labels'
const data = ref(null)
const err = ref('')
onMounted(async () => {
  try { data.value = await api.overview() } catch (e) { err.value = e.message }
})
</script>
<template>
  <div>
    <h1>总览</h1>
    <p class="sub">当前账期估抄、实抄、对齐与封印状态</p>
    <p v-if="err" class="msg err">{{ err }}</p>
    <div class="grid" v-if="data">
      <div class="stat">当前账期<b>{{ data.current_period }}</b></div>
      <div class="stat">开放账期<b>{{ data.open_periods }}</b></div>
      <div class="stat">已封印账期<b>{{ data.sealed_periods }}</b></div>
      <div class="stat">表具数<b>{{ data.meter_count }}</b></div>
      <div class="stat">未决差额<b>{{ data.pending_variances }}</b></div>
    </div>
    <div class="panel" v-if="data && data.coverage">
      <h3>覆盖率（当前账期）</h3>
      <div class="grid">
        <div class="stat">口径<b>{{ 标(覆盖架构, data.coverage.architecture) }}</b></div>
        <div class="stat">覆盖率<b>{{ 覆盖比(data.coverage.coverage_ratio) }}</b></div>
        <div class="stat">阈值<b>{{ 覆盖比(data.coverage.threshold) }}</b></div>
        <div class="stat">缺失表<b>{{ data.coverage.missing_meters.length }}</b></div>
        <div class="stat">状态<b>{{ 标(覆盖状态, data.coverage.state) }}</b></div>
      </div>
      <p class="msg" v-if="data.coverage.period_status !== 'sealed'">
        封印闸：{{ data.coverage.gate_ok ? '可封' : '不可封 — ' + (data.coverage.gate_reason || '') }}
      </p>
      <p class="msg" v-if="data.coverage.active_precheck_stale === true">
        当前预检已过期，请重新扫描覆盖率。
      </p>
      <p class="msg" v-if="data.coverage.missing_meters.length">
        缺失：{{ data.coverage.missing_meters.join('、') }}
        <router-link to="/coverage/missing">（清单）</router-link>
      </p>
    </div>
    <div class="panel" v-if="data">
      <h3>最近对账批</h3>
      <table>
        <thead><tr><th>批号</th><th>账期</th><th>匹配</th><th>差额</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="b in data.recent_batches" :key="b.batch_id">
            <td>{{ b.batch_id }}</td>
            <td>{{ b.period_id }}</td>
            <td>{{ b.matched_count }}</td>
            <td>{{ b.variance_count }}</td>
            <td><span class="tag" :class="b.status">{{ 标(批状态, b.status) }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
