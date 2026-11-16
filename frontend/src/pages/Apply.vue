<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const clock = ref('')
const batches = ref([])
const msg = ref('')
const err = ref('')
const result = ref(null)

async function refresh() {
  const c = await api.clock()
  clock.value = c.business_clock || ''
  batches.value = await api.batches()
}

async function saveClock() {
  msg.value = ''
  err.value = ''
  try {
    await api.setClock(clock.value)
    msg.value = '业务钟已更新'
    await refresh()
  } catch (e) {
    err.value = e.message
  }
}

async function applyDue() {
  msg.value = ''
  err.value = ''
  result.value = null
  try {
    result.value = await api.applyDue()
    msg.value = result.value.applied
      ? `已应用 ${result.value.ticket_count} 张改价单`
      : result.value.message
    await refresh()
  } catch (e) {
    err.value = e.message
  }
}

onMounted(() => refresh().catch((e) => (err.value = e.message)))
</script>

<template>
  <section class="hero">
    <h1>生效应用</h1>
    <p>拨动业务钟后，点「应用到期」将 effective_at ≤ 业务钟的改价单写入现行价签。</p>
  </section>

  <div class="panel">
    <h2>业务钟</h2>
    <div class="row">
      <div class="field" style="flex:1">
        <label>业务钟 (UTC)</label>
        <input v-model="clock" />
      </div>
      <button class="btn ghost" type="button" @click="saveClock">保存业务钟</button>
      <button class="btn warn" type="button" @click="applyDue">应用到期</button>
    </div>
    <p v-if="msg" class="msg ok">{{ msg }}</p>
    <p v-if="err" class="msg err">{{ err }}</p>
    <pre v-if="result" style="margin-top:12px;font-size:0.82rem;color:var(--muted);white-space:pre-wrap">{{ JSON.stringify(result, null, 2) }}</pre>
    <div v-if="result && result.skipped_gated && result.skipped_gated.length" class="panel" style="border-left-color:var(--warn)">
      <h2>禁闸拦截（普通单未应用，保持待生效）</h2>
      <table>
        <thead><tr><th>改价单</th><th>禁闸 SKU</th><th>误用标记</th></tr></thead>
        <tbody>
          <tr v-for="s in result.skipped_gated" :key="s.ticket_id">
            <td>{{ s.ticket_no }} (#{{ s.ticket_id }})</td>
            <td>{{ s.blocked_skus.join(', ') }}</td>
            <td>{{ s.markers.join(', ') || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="msg">请先到「误用纠偏」页让指向该标记的补偿单到期应用；闸解除后下一轮应用到期会自动处理这些单。</p>
    </div>
    <div v-if="result && result.resolved_marker_ids && result.resolved_marker_ids.length" class="msg ok">
      本次应用已解除误用标记：#{{ result.resolved_marker_ids.join(', #') }}
    </div>
  </div>

  <div class="panel">
    <h2>应用批次</h2>
    <table>
      <thead>
        <tr><th>批次</th><th>业务钟</th><th>改价单数</th><th>明细行</th><th>状态</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="b in batches" :key="b.id">
          <td>{{ b.batch_no }}</td>
          <td>{{ b.clock_at }}</td>
          <td>{{ b.ticket_count }}</td>
          <td>{{ b.line_count }}</td>
          <td><span class="tag ok">{{ b.status }}</span></td>
          <td>{{ b.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
