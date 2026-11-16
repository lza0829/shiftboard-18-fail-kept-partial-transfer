<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const filter = ref('全部')
const err = ref('')
const openId = ref(null)

const shown = computed(() => {
  if (filter.value === '全部') return rows.value
  return rows.value.filter((r) => r.结论 === filter.value)
})

const stats = computed(() => {
  const ok = rows.value.filter((r) => r.一致).length
  const fail = rows.value.length - ok
  return { ok, fail }
})

function load() {
  err.value = ''
  return getJson('/api/reconcile')
    .then((data) => { rows.value = data })
    .catch((e) => { err.value = String(e.message || e) })
}

onMounted(load)

function toggle(id) {
  openId.value = openId.value === id ? null : id
}
</script>

<template>
  <div>
    <h1>交班对账</h1>
    <p class="lead">
      只有整单成功的交班才落「一致」：交班方在途归 0、接班方接住全部清单批次。
      失败回滚单落「失败已回滚」，数字取自回滚后库内行，并列出回滚后交班方在途批次供与发起清单核对。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>

    <div class="stats">
      <div class="stat"><div class="k">成功一致</div><div class="v">{{ stats.ok }}</div></div>
      <div class="stat"><div class="k">失败已回滚</div><div class="v">{{ stats.fail }}</div></div>
      <div class="stat">
        <div class="k">筛选</div>
        <div class="row" style="margin:6px 0 0">
          <button
            v-for="opt in ['全部', '成功', '失败已回滚']"
            :key="opt"
            class="btn ghost"
            :class="{ active: filter === opt }"
            @click="filter = opt"
          >{{ opt }}</button>
        </div>
      </div>
    </div>

    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>报告</th><th>交班单</th><th>方向</th><th>结论</th>
            <th>发起清单</th><th>交后交班方</th><th>交后接班方</th><th>一致</th><th></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="r in shown" :key="r.id">
            <tr>
              <td>#{{ r.id }}</td>
              <td>#{{ r.handover_id }} · {{ r.handover_status || '—' }}</td>
              <td>{{ r.from_shift_name || '—' }} → {{ r.to_shift_name || '—' }}</td>
              <td>
                <span class="tag" :class="{ on: r.一致, bad: !r.一致 }">{{ r.结论 }}</span>
              </td>
              <td>{{ r.before_count }}</td>
              <td>{{ r.after_from_count }}</td>
              <td>{{ r.after_to_count }}</td>
              <td>
                <span class="tag" :class="{ on: r.一致, bad: !r.一致 }">{{ r.一致 ? '一致' : '不一致' }}</span>
              </td>
              <td><button class="btn ghost" @click="toggle(r.id)">{{ openId === r.id ? '收起' : '详情' }}</button></td>
            </tr>
            <tr v-if="openId === r.id">
              <td colspan="9">
                <div class="detail">
                  <p v-if="r.detail.失败原因">失败原因：{{ r.detail.失败原因 }}</p>
                  <p>发起清单批次：{{ (r.detail.发起清单批次 || []).join('、') || '—' }}</p>
                  <div v-if="!r.一致" class="proof">
                    <h3>回滚证明</h3>
                    <p class="proofline">
                      <span class="tag" :class="{ on: r.detail['回滚后交班方在途与发起清单一致'], bad: !r.detail['回滚后交班方在途与发起清单一致'] }">
                        回滚后交班方在途与发起清单一致：{{ r.detail['回滚后交班方在途与发起清单一致'] ? '是' : '否' }}
                      </span>
                    </p>
                    <p>回滚后交班方在途批次：{{ (r.detail.回滚后交班方在途批次 || []).join('、') || '—' }}
                      （{{ r.detail.回滚后交班方在途批次数 ?? 0 }} 批）</p>
                    <p>发起前归属指纹：<code>{{ r.detail.发起前归属指纹 || '—' }}</code></p>
                    <p>
                      归属回到发起快照：{{ r.detail.归属已回滚到发起快照 === null ? '无快照，无法核对' : (r.detail.归属已回滚到发起快照 ? '是' : '否') }} ·
                      状态回到发起快照：{{ r.detail.状态已回到发起快照 === null ? '无快照，无法核对' : (r.detail.状态已回到发起快照 ? '是' : '否') }} ·
                      回滚方式：{{ r.detail.回滚方式 || '—' }}
                    </p>
                    <p v-if="r.detail.锁释放结果">
                      锁释放结果：{{ r.detail.锁释放结果.进行中锁 }} ·
                      全仓进行中交班数 {{ r.detail.锁释放结果.全仓进行中交班数 }} ·
                      双方可再次交班：{{ r.detail.锁释放结果.双方可再次交班 ? '是' : '否' }}
                    </p>
                  </div>
                  <p v-if="r.一致">
                    清单已全部过户：{{ r.detail.清单已全部过户 ? '是' : '否' }} ·
                    交班方无残留在途：{{ r.detail.交班方无残留在途 ? '是' : '否' }}
                  </p>
                  <p v-if="r.detail.班次状态恢复">
                    班次状态恢复：<span v-for="(v, k) in r.detail.班次状态恢复" :key="k">#{{ k }} → {{ v }}　</span>
                  </p>
                  <p class="muted">报告生成：{{ r.created_at }}</p>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!shown.length">
            <td colspan="9" class="muted">暂无对账报告。</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
