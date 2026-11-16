<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'

const strategy = ref('manual')
const clock = ref('')
const tickets = ref([])
const batches = ref([])
const markers = ref([])
const gates = ref({ blocked_skus: [], gates: [] })
const msg = ref('')
const err = ref('')

const markForm = reactive({ target_type: 'ticket', target_id: '', reason: '' })
const compForm = reactive({ marker_id: '', effective_at: '', lines: [] })
const lineDraft = reactive({ sku_code: '', new_price: '' })

const openMarkers = computed(() => markers.value.filter((m) => m.status === 'open'))

async function refresh() {
  const [s, c, ts, bs, ms, gs] = await Promise.all([
    api.compStrategy(),
    api.clock(),
    api.tickets(),
    api.batches(),
    api.markers(),
    api.gates(),
  ])
  strategy.value = s.compensation_strategy
  clock.value = c.business_clock
  tickets.value = ts
  batches.value = bs
  markers.value = ms
  gates.value = gs
}

async function saveStrategy(value) {
  msg.value = ''
  err.value = ''
  try {
    const r = await api.setCompStrategy(value)
    strategy.value = r.compensation_strategy
    msg.value = `全局补偿策略已切换为：${r.compensation_strategy === 'auto_draft' ? '自动草稿' : '仅人工建补偿单'}`
  } catch (e) {
    err.value = e.message
  }
}

async function submitMark() {
  msg.value = ''
  err.value = ''
  if (!markForm.target_id) {
    err.value = '请填写要纠偏的已应用单 ID 或批次 ID'
    return
  }
  try {
    const r = await api.markMisapply({
      target_type: markForm.target_type,
      target_id: Number(markForm.target_id),
      reason: markForm.reason || null,
    })
    const parts = [`已创建误用标记 ${r.marker.marker_no}`]
    if (r.draft_ticket) parts.push(`并生成补偿草稿 ${r.draft_ticket.ticket_no}（到期后到「生效应用」页应用）`)
    else parts.push('相关商品已进入禁闸，等待人工补偿单')
    msg.value = parts.join('，')
    markForm.target_id = ''
    markForm.reason = ''
    await refresh()
  } catch (e) {
    err.value = e.message
  }
}

function selectMarker(id) {
  compForm.marker_id = String(id)
  const m = markers.value.find((x) => x.id === id)
  compForm.lines = (m?.sku_codes || []).map((sku) => ({ sku_code: sku, new_price: '' }))
  compForm.effective_at = clock.value
}

function addCompLine() {
  if (!lineDraft.sku_code || lineDraft.new_price === '') {
    err.value = '请选择禁闸商品并填写纠偏价'
    return
  }
  if (compForm.lines.some((l) => l.sku_code === lineDraft.sku_code)) {
    err.value = '该商品已在补偿明细中'
    return
  }
  compForm.lines.push({ sku_code: lineDraft.sku_code, new_price: Number(lineDraft.new_price) })
  lineDraft.sku_code = ''
  lineDraft.new_price = ''
  err.value = ''
}

function removeCompLine(i) {
  compForm.lines.splice(i, 1)
}

async function submitComp() {
  msg.value = ''
  err.value = ''
  if (!compForm.marker_id) {
    err.value = '请先选择一个未解除的误用标记'
    return
  }
  if (!compForm.lines.length) {
    err.value = '至少一行补偿明细'
    return
  }
  try {
    const t = await api.createCompensationTicket({
      marker_id: Number(compForm.marker_id),
      effective_at: compForm.effective_at,
      lines: compForm.lines.map((l) => ({ sku_code: l.sku_code, new_price: Number(l.new_price) })),
    })
    msg.value = `已创建人工补偿单 ${t.ticket_no}，生效时刻 ${t.effective_at}；到期后到「生效应用」页应用`
    compForm.marker_id = ''
    compForm.lines = []
    await refresh()
  } catch (e) {
    err.value = e.message
  }
}

function statusTag(status) {
  return status === 'resolved' ? 'ok' : 'warn'
}

onMounted(() => refresh().catch((e) => (err.value = e.message)))
</script>

<template>
  <section class="hero">
    <h1>误用纠偏（向前补偿）</h1>
    <p>
      已成功应用的价签写入与历史行不可删除、不可回滚。纠偏只能新开补偿改价单，走同一套到期应用规则向前修正；
      被标记商品在禁闸解除前禁止应用任何非补偿单。
    </p>
  </section>

  <p v-if="err" class="msg err">{{ err }}</p>
  <p v-if="msg" class="msg ok">{{ msg }}</p>

  <div class="panel">
    <h2>补偿发起策略（全局互斥，二选一）</h2>
    <div class="row">
      <button
        class="btn"
        :class="{ ghost: strategy !== 'auto_draft' }"
        type="button"
        @click="saveStrategy('auto_draft')"
      >
        自动草稿
      </button>
      <button
        class="btn"
        :class="{ ghost: strategy !== 'manual' }"
        type="button"
        @click="saveStrategy('manual')"
      >
        仅人工建补偿单
      </button>
      <span class="msg">当前：<strong>{{ strategy === 'auto_draft' ? '自动草稿' : '仅人工建补偿单' }}</strong>（切换只影响之后新建的标记）</span>
    </div>
    <p class="msg">
      自动草稿：标记已应用单/批次后系统生成补偿草稿（反向价），仍须到期应用；草稿应用前相关商品禁止其他非补偿单。<br />
      仅人工：系统不造草稿，被标记商品加禁应用闸，直至存在指向该标记的已应用补偿单才解除。
    </p>
  </div>

  <div class="panel">
    <h2>标记误用（只能针对已成功应用的单或批次）</h2>
    <div class="row">
      <div class="field">
        <label>纠偏对象类型</label>
        <select v-model="markForm.target_type">
          <option value="ticket">改价单（ticket id）</option>
          <option value="batch">应用批次（batch id）</option>
        </select>
      </div>
      <div class="field">
        <label>对象 ID</label>
        <input v-model="markForm.target_id" type="number" placeholder="例如 1" />
      </div>
      <div class="field" style="flex:1">
        <label>误用原因</label>
        <input v-model="markForm.reason" placeholder="例如：新价填错，需恢复原价" />
      </div>
      <button class="btn warn" type="button" @click="submitMark">标记误用并按策略发起补偿</button>
    </div>
    <p class="msg">
      可选已应用单：
      <span v-for="t in tickets.filter((x) => x.status === 'applied')" :key="t.id" style="margin-right:10px">
        #{{ t.id }} {{ t.ticket_no }}
      </span>
      <span v-if="!tickets.some((x) => x.status === 'applied')">暂无已应用单</span>
    </p>
    <p class="msg">
      可选批次：
      <span v-for="b in batches" :key="b.id" style="margin-right:10px">
        #{{ b.id }} {{ b.batch_no }}（{{ b.ticket_count }} 单）
      </span>
    </p>
  </div>

  <div class="panel">
    <h2>禁应用闸（源自未解除的误用标记，与价签/历史/对账同源）</h2>
    <p v-if="!gates.blocked_skus.length" class="msg ok">当前无禁闸商品。</p>
    <table v-else>
      <thead>
        <tr><th>标记</th><th>对象</th><th>策略</th><th>禁闸 SKU</th><th>原因</th><th>创建时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="g in gates.gates" :key="g.marker_id">
          <td>{{ g.marker_no }} (#{{ g.marker_id }})</td>
          <td>{{ g.target_type }} #{{ g.target_ticket_id ?? g.target_batch_id }}</td>
          <td>{{ g.strategy === 'auto_draft' ? '自动草稿' : '仅人工' }}</td>
          <td>{{ g.blocked_skus.join(', ') }}</td>
          <td>{{ g.reason || '—' }}</td>
          <td>{{ g.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="panel" v-if="strategy === 'manual'">
    <h2>人工新建补偿单（目标价=纠偏价，生效时刻须 ≥ 当前业务钟 {{ clock }}）</h2>
    <div class="row">
      <div class="field">
        <label>指向的未解除标记</label>
        <select :value="compForm.marker_id" @change="(e) => selectMarker(Number(e.target.value))">
          <option value="">请选择…</option>
          <option v-for="m in openMarkers" :key="m.id" :value="m.id">
            {{ m.marker_no }} #{{ m.id }}（{{ m.sku_codes.join(', ') }}）
          </option>
        </select>
      </div>
      <div class="field">
        <label>生效时刻 (UTC)</label>
        <input v-model="compForm.effective_at" />
      </div>
    </div>
    <div class="row">
      <div class="field">
        <label>禁闸商品</label>
        <select v-model="lineDraft.sku_code">
          <option value="">请选择…</option>
          <option
            v-for="sku in gates.blocked_skus.filter((s) => !compForm.lines.some((l) => l.sku_code === s))"
            :key="sku"
            :value="sku"
          >{{ sku }}</option>
        </select>
      </div>
      <div class="field">
        <label>纠偏目标价</label>
        <input v-model="lineDraft.new_price" type="number" step="0.01" />
      </div>
      <button class="btn ghost" type="button" @click="addCompLine">加入补偿明细</button>
    </div>
    <table v-if="compForm.lines.length">
      <thead><tr><th>SKU</th><th>纠偏价</th><th></th></tr></thead>
      <tbody>
        <tr v-for="(l, i) in compForm.lines" :key="l.sku_code">
          <td>{{ l.sku_code }}</td>
          <td><input v-model="l.new_price" type="number" step="0.01" /></td>
          <td><button class="btn ghost" type="button" @click="removeCompLine(i)">移除</button></td>
        </tr>
      </tbody>
    </table>
    <div class="row" style="margin-top:12px">
      <button class="btn" type="button" @click="submitComp">提交补偿单（草稿，到期才写入价签）</button>
    </div>
  </div>

  <div class="panel">
    <h2>误用标记与补偿单历史</h2>
    <table>
      <thead>
        <tr><th>标记</th><th>对象</th><th>SKU</th><th>策略</th><th>状态</th><th>关联补偿单</th><th>解除时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="m in markers" :key="m.id">
          <td>{{ m.marker_no }} (#{{ m.id }})</td>
          <td>{{ m.target_type }} #{{ m.target_ticket_id ?? m.target_batch_id }}</td>
          <td>{{ m.sku_codes.join(', ') }}</td>
          <td>{{ m.strategy === 'auto_draft' ? '自动草稿' : '仅人工' }}</td>
          <td><span class="tag" :class="statusTag(m.status)">{{ m.status }}</span></td>
          <td>
            <span v-for="t in m.compensation_tickets" :key="t.id" style="margin-right:8px">
              {{ t.ticket_no }}
              <span class="tag muted">{{ t.compensation_mode }}</span>
              <span class="tag" :class="t.status === 'applied' ? 'ok' : 'warn'">{{ t.status }}</span>
            </span>
            <span v-if="!m.compensation_tickets.length">—</span>
          </td>
          <td>{{ m.resolved_at || '—' }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
