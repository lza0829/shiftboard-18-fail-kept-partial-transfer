<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')
const filter = ref('全部')

const shown = computed(() => {
  if (filter.value === '全部') return rows.value
  if (filter.value === '成功') return rows.value.filter((h) => h.status === '已完成')
  return rows.value.filter((h) => h.status === '失败')
})

function load() {
  err.value = ''
  return getJson('/api/handovers')
    .then((data) => { rows.value = data })
    .catch((e) => { err.value = String(e.message || e) })
}

onMounted(load)

function cls(status) {
  if (status === '已完成') return 'on'
  if (status === '失败') return 'bad'
  return ''
}
</script>

<template>
  <div>
    <h1>交班记录</h1>
    <p class="lead">每单交班留档：双方班次、清单批次数、最终状态；失败单同时记录原因与回滚结论。</p>
    <p v-if="err" class="msg">{{ err }}</p>

    <div class="panel">
      <div class="row">
        <button
          v-for="opt in ['全部', '成功', '失败']"
          :key="opt"
          class="btn ghost"
          :class="{ active: filter === opt }"
          @click="filter = opt"
        >{{ opt }}</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>单号</th><th>交班方</th><th>接班方</th><th>批次数</th>
            <th>状态</th><th>原因</th><th>开始</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="h in shown" :key="h.id">
            <td>#{{ h.id }}</td>
            <td>{{ h.from_shift_name }}</td>
            <td>{{ h.to_shift_name }}</td>
            <td>{{ h.batch_count }}</td>
            <td><span class="tag" :class="cls(h.status)">{{ h.status }}</span></td>
            <td class="muted">{{ h.reason || '—' }}</td>
            <td>{{ h.started_at }}</td>
            <td>{{ h.finished_at || '—' }}</td>
          </tr>
          <tr v-if="!shown.length">
            <td colspan="8" class="muted">暂无交班单。</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
