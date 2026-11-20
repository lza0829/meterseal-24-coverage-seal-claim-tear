<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const current = ref('')
const architecture = ref('hard_gate')
const threshold = ref(80)
const msg = ref('')
const err = ref('')

async function load() {
  const s = await api.settings()
  current.value = s.current_period || ''
  architecture.value = s.coverage_architecture || 'hard_gate'
  const t = Number(s.coverage_threshold)
  threshold.value = Number.isFinite(t) ? Math.round(t * 100) : 80
}
async function saveCurrent() {
  err.value = ''; msg.value = ''
  try {
    await api.putSetting('current_period', current.value)
    msg.value = '已保存当前账期'
    await load()
  } catch (e) { err.value = e.message }
}
async function saveCoverage() {
  err.value = ''; msg.value = ''
  try {
    await api.putCoveragePolicy({ architecture: architecture.value, threshold: threshold.value / 100 })
    msg.value = '已保存覆盖率口径；旧预检将失效，封印前需重新扫描'
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<template>
  <div>
    <h1>设置</h1>
    <p class="sub">运行参数与覆盖率口径</p>
    <div class="panel">
      <h3>当前账期</h3>
      <div class="row">
        <label>当前账期<input v-model="current" /></label>
        <button @click="saveCurrent">保存</button>
      </div>
    </div>
    <div class="panel">
      <h3>覆盖率口径</h3>
      <p class="msg">
        全局只贯彻一种架构；切换架构或收紧阈值后，旧预检立即失效，封印前须重新扫描。
      </p>
      <div class="row">
        <label>架构
          <select v-model="architecture">
            <option value="hard_gate">覆盖率硬闸（不足拒封）</option>
            <option value="allow_skip">允许低覆盖直封（可跳过，但不得伪造达标）</option>
          </select>
        </label>
        <label>阈值（%）<input type="number" min="0" max="100" step="1" v-model.number="threshold" /></label>
        <button @click="saveCoverage">保存口径</button>
      </div>
      <p class="msg err" v-if="err">{{ err }}</p>
      <p class="msg ok" v-if="msg">{{ msg }}</p>
    </div>
  </div>
</template>
