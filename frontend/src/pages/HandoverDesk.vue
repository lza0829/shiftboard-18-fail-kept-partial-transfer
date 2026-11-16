<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const shifts = ref([])
const fromId = ref(1)
const toId = ref(2)
const logs = ref([])
const current = ref(null)
const lastResult = ref(null)
const msg = ref('')
const ok = ref(false)

async function refresh() {
  shifts.value = await getJson('/api/shifts')
  logs.value = await getJson('/api/handovers')
  const active = logs.value.find((h) => h.status === '进行中')
  current.value = active ? await getJson(`/api/handovers/${active.id}`) : null
  if (lastResult.value) {
    lastResult.value = await getJson(`/api/handovers/${lastResult.value.id}`)
  }
}

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
})

async function start() {
  msg.value = ''
  ok.value = false
  lastResult.value = null
  try {
    const created = await postJson('/api/handovers/start', {
      from_shift_id: Number(fromId.value),
      to_shift_id: Number(toId.value),
    })
    ok.value = true
    msg.value = `已开交班单 #${created.id}，清单 ${created.batch_count} 批`
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}

async function complete() {
  if (!current.value) return
  msg.value = ''
  ok.value = false
  try {
    const result = await postJson(`/api/handovers/${current.value.id}/complete`)
    lastResult.value = result
    if (result.status === '已完成') {
      current.value = null
      ok.value = true
      msg.value = `交班单 #${result.id} 已完成：${result.batch_count} 批全部过户到${result.to_shift_name}`
    } else {
      ok.value = false
      msg.value = `交班单 #${result.id} 失败：${result.reason || '归属校验未通过'}`
    }
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
    await refresh()
  }
}

function badgeClass(status) {
  if (status === '已完成') return 'on'
  if (status === '失败') return 'bad'
  return ''
}
</script>

<template>
  <div>
    <h1>交班办理</h1>
    <p class="lead">
      同一时刻只允许一单进行中的交班。完成交班在同一数据库事务内整单过户：清单内全部在途批次过户、
      交班单标完成、对账报告落档三者要么全成，要么全部回滚，失败单不占「进行中」锁。
    </p>

    <div class="panel">
      <div class="row">
        <label>交班方
          <select v-model.number="fromId">
            <option v-for="s in shifts" :key="s.id" :value="s.id">{{ s.name }} · {{ s.keeper }}（{{ s.status }}）</option>
          </select>
        </label>
        <label>接班方
          <select v-model.number="toId">
            <option v-for="s in shifts" :key="'t'+s.id" :value="s.id">{{ s.name }} · {{ s.keeper }}（{{ s.status }}）</option>
          </select>
        </label>
        <button class="btn" :disabled="!!current" @click="start">发起交班</button>
        <button class="btn ghost" :disabled="!current" @click="complete">完成交班</button>
      </div>
      <p class="msg" :class="{ ok }">{{ msg }}</p>
    </div>

    <div class="panel" v-if="current">
      <h2>当前交班单 #{{ current.id }} · {{ current.status }}</h2>
      <p class="lead" style="margin-bottom:12px">
        {{ current.from_shift_name }} → {{ current.to_shift_name }} · 清单 {{ current.batch_count }} 批
      </p>
      <table>
        <thead>
          <tr><th>业务键</th><th>标题</th><th>发起前归属</th><th>当前归属</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in current.items || []" :key="b.id">
            <td>{{ b.biz_key }}</td>
            <td>{{ b.title }}</td>
            <td>{{ b.before_owner_shift_name || '—' }}</td>
            <td>{{ b.owner_shift_name || '—' }}</td>
            <td>{{ b.status }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="lastResult">
      <h2>
        本次处理结果 #{{ lastResult.id }} ·
        <span class="tag" :class="badgeClass(lastResult.status)">{{ lastResult.status }}</span>
      </h2>
      <p class="lead" style="margin-bottom:12px">
        {{ lastResult.from_shift_name }} → {{ lastResult.to_shift_name }} · 清单 {{ lastResult.batch_count }} 批
        <template v-if="lastResult.reason"> · 原因：{{ lastResult.reason }}</template>
      </p>
      <template v-if="lastResult.report">
        <p class="lead" style="margin-bottom:12px">
          对账：交班前清单 {{ lastResult.report.before_count }} 批 ·
          交后交班方在途 {{ lastResult.report.after_from_count }} 批 ·
          交后接班方在途 {{ lastResult.report.after_to_count }} 批 ·
          <span class="tag" :class="{ on: lastResult.report.一致, bad: !lastResult.report.一致 }">
            {{ lastResult.report.结论 }}
          </span>
        </p>
      </template>
      <table>
        <thead>
          <tr><th>业务键</th><th>标题</th><th>发起前归属</th><th>处理后在途归属</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in lastResult.items || []" :key="b.id">
            <td>{{ b.biz_key }}</td>
            <td>{{ b.title }}</td>
            <td>{{ b.before_owner_shift_name || '—' }}</td>
            <td>{{ b.owner_shift_name || '—' }}</td>
            <td>{{ b.status }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="lastResult.status === '失败'" class="msg">
        归属已回滚到发起前快照，双方班次恢复可再次交班，可重新发起。
      </p>
    </div>
  </div>
</template>
