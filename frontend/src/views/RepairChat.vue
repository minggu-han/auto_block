<template>
  <section class="chat-layout conversation-layout">
    <aside class="history-panel">
      <div class="history-header">
        <h3>聊天记录</h3>
        <button @click="loadSessions">刷新</button>
      </div>
      <p v-if="historyError" class="error">{{ historyError }}</p>
      <div v-if="!historyError && sessions.length===0" class="muted">暂无聊天记录</div>
      <button
        v-for="s in sessions"
        :key="s.id"
        :class="['history-item', { active: s.id === currentSessionId }]"
        @click="openSession(s.id)"
      >
        <b>#{{ s.id }} {{ s.title }}</b>
        <small>{{ statusLabel(s.status) }} · {{ nodeLabel(s.current_node) }} · {{ s.message_count }} 条</small>
        <em v-if="s.missing_punch_id">补卡 #{{ s.missing_punch_id }} ｜ {{ s.repair_work_date || '未知日期' }} ｜ {{ labelPunch(s.repair_punch_type) }}</em>
        <span>{{ s.last_message || '暂无消息' }}</span>
      </button>
    </aside>

    <div class="chat-panel">
      <h2>补卡聊天</h2>

      <div class="target-card">
        <span class="flow-title">本次会话针对的补卡数据</span>
        <div v-if="currentRepairRecord" class="target-main">
          <b>编号 #{{ currentRepairRecord.id }}</b>
          <b>{{ currentRepairRecord.work_date || '未知日期' }}</b>
          <b>{{ currentRepairRecord.punch_type_label || labelPunch(currentRepairRecord.punch_type) }}</b>
          <b v-if="currentRepairRecord.status">{{ currentRepairRecord.status }}</b>
        </div>
        <p v-else>尚未选择补卡数据。请在聊天中说明“我要补卡”，然后按提示选择一条数据。</p>
      </div>

      <div class="flow-card">
        <div>
          <span class="flow-title">当前流程节点</span>
          <strong>{{ nodeLabel(currentNode) }}</strong>
        </div>
        <div class="flow-status" :class="{ done: sessionEnded }">{{ sessionEnded ? '本轮对话已结束，请新建会话' : '本轮对话进行中' }}</div>
      </div>

      <div v-if="sessionEnded" :class="['result-card', resultClass]">
        <div class="result-icon">{{ resultIcon }}</div>
        <div>
          <span class="flow-title">本轮补卡结果</span>
          <strong>{{ resultTitle }}</strong>
          <div v-if="currentRepairRecord" class="result-record-detail">
            <div><span>补卡记录编号</span><b>#{{ currentRepairRecord.id }}</b></div>
            <div><span>补卡日期</span><b>{{ currentRepairRecord.work_date || '未知日期' }}</b></div>
            <div><span>补卡类型</span><b>{{ currentRepairRecord.punch_type_label || labelPunch(currentRepairRecord.punch_type) }}</b></div>
            <div v-if="currentRepairRecord.status"><span>记录状态</span><b>{{ currentRepairRecord.status }}</b></div>
          </div>
          <p>{{ resultMessage }}</p>
        </div>
      </div>

      <p v-if="detailError" class="error">{{ detailError }}</p>
      <div class="messages" ref="box">
        <div v-for="(m,i) in messages" :key="i" :class="['msg', m.role]"><pre>{{ m.content }}</pre></div>
      </div>
      <form class="composer" @submit.prevent="send">
        <input v-model="input" :disabled="loading || sessionEnded" :placeholder="sessionEnded ? '本轮对话已结束，请点击新会话' : '例如：我要补卡 / 选第1条 / 补编号12'" />
        <button :disabled="loading || sessionEnded || !input.trim()">{{ loading ? '理解中...' : '发送' }}</button>
        <button type="button" class="secondary" :disabled="loading" @click="newChat">新会话</button>
      </form>
    </div>
  </section>
</template>
<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { api, streamPost } from '../api/client'

const welcome = '你好，我是自动补卡 Agent。你可以直接说“我要补卡”，我会理解你的意思、调用工具查询可补卡数据，并在聊天框里让你选择。'
const messages = ref([{ role:'agent', content: welcome }])
const sessions = ref([])
const historyError = ref('')
const detailError = ref('')
const currentSessionId = ref(null)
const currentNode = ref('start')
const currentStatus = ref('ACTIVE')
const currentResultStatus = ref('IN_PROGRESS')
const currentResultMessage = ref('')
const currentRepairRecord = ref(null)
const input = ref('')
const loading = ref(false)
const box = ref(null)

const sessionEnded = computed(() => currentNode.value === 'end' || ['DONE', 'WAITING_APPROVAL', 'FAILED'].includes(currentStatus.value))
const resultTitle = computed(() => ({ SUCCESS:'补卡成功', PENDING_APPROVAL:'等待领导审核', FAILED:'补卡失败', IN_PROGRESS:'处理中' })[currentResultStatus.value] || deriveResultTitle())
const resultIcon = computed(() => ({ SUCCESS:'✅', PENDING_APPROVAL:'🕒', FAILED:'❌', IN_PROGRESS:'⏳' })[currentResultStatus.value] || 'ℹ️')
const resultClass = computed(() => ({ SUCCESS:'success', PENDING_APPROVAL:'pending', FAILED:'failed' })[currentResultStatus.value] || deriveResultClass())
const resultMessage = computed(() => currentResultMessage.value || deriveResultMessage())

function statusLabel(s){ return ({ ACTIVE:'进行中', DONE:'已完成', WAITING_APPROVAL:'待领导审核', FAILED:'失败' })[s] || s }
function labelPunch(t){ if (!t) return '未知类型'; return t === 'ON_DUTY' ? '上班卡' : '下班卡' }
function nodeLabel(n){
  return ({
    start:'开始', query_missing_punches:'查询可补卡数据',
    waiting_user_selection:'等待用户选择补卡数据', waiting_user_intent:'等待用户说明补卡需求',
    repair_missing_punch:'执行补卡（自动补卡 / 提交审核）', end:'流程结束'
  })[n] || n || '开始'
}
function resultStatusFromSession(status, node){
  if (status === 'DONE') return 'SUCCESS'
  if (status === 'WAITING_APPROVAL') return 'PENDING_APPROVAL'
  if (status === 'FAILED' || node === 'end') return 'FAILED'
  return 'IN_PROGRESS'
}
function deriveResultTitle(){ return ({ DONE:'补卡成功', WAITING_APPROVAL:'等待领导审核', FAILED:'补卡失败' })[currentStatus.value] || '处理中' }
function deriveResultClass(){ return ({ DONE:'success', WAITING_APPROVAL:'pending', FAILED:'failed' })[currentStatus.value] || '' }
function deriveResultMessage(){
  if (currentStatus.value === 'DONE') return '补卡成功，当前记录已自动补卡完成。'
  if (currentStatus.value === 'WAITING_APPROVAL') return '已提交领导审核，领导通过后会自动补卡。'
  if (currentStatus.value === 'FAILED' || currentNode.value === 'end') return '补卡失败，请查看聊天内容中的失败原因。'
  return '本轮对话尚未结束，请继续按提示操作。'
}
function recordFromDetail(data){
  return data.missing_punch_id ? {
    id: data.missing_punch_id,
    work_date: data.repair_work_date,
    punch_type: data.repair_punch_type,
    punch_type_label: labelPunch(data.repair_punch_type),
    status: data.repair_status
  } : null
}

async function loadSessions(){
  historyError.value = ''
  try { sessions.value = (await api.get('/chat-sessions')).data }
  catch(e){ historyError.value = e.response?.data?.detail || e.message || '聊天记录加载失败'; sessions.value = [] }
}
async function openSession(id){
  detailError.value = ''
  try {
    const { data } = await api.get(`/chat-sessions/${id}`)
    currentSessionId.value = data.id
    currentNode.value = data.current_node || 'start'
    currentStatus.value = data.status || 'ACTIVE'
    currentResultStatus.value = resultStatusFromSession(currentStatus.value, currentNode.value)
    currentResultMessage.value = ''
    currentRepairRecord.value = recordFromDetail(data)
    messages.value = (data.messages || []).map(m => ({ role: m.role || 'agent', content: m.content || '' }))
    if (messages.value.length === 0) messages.value = [{ role:'agent', content:'这条聊天记录暂无消息内容。' }]
    await scrollBottom()
  } catch(e) {
    detailError.value = e.response?.data?.detail || e.message || '聊天详情加载失败'
  }
}
async function scrollBottom(){ await nextTick(); box.value && (box.value.scrollTop = box.value.scrollHeight) }
async function push(role, content){ messages.value.push({ role, content }); await scrollBottom() }
function handleEvent(eventName, data){
  if (data.type === 'session' && data.session_id) currentSessionId.value = data.session_id
  if (data.type === 'node' && data.node) currentNode.value = data.node
  if (data.type === 'done') {
    if (data.current_node) currentNode.value = data.current_node
    if (data.status) currentStatus.value = data.status
    currentResultStatus.value = data.result_status || resultStatusFromSession(currentStatus.value, currentNode.value)
    currentResultMessage.value = data.result_message || ''
    if (data.repair_record) currentRepairRecord.value = data.repair_record
  }
}
function newChat(){
  currentSessionId.value = null
  currentNode.value = 'start'
  currentStatus.value = 'ACTIVE'
  currentResultStatus.value = 'IN_PROGRESS'
  currentResultMessage.value = ''
  currentRepairRecord.value = null
  detailError.value = ''
  messages.value = [{ role:'agent', content: welcome }]
  input.value = ''
}
async function send(){
  const text = input.value.trim()
  if (!text || sessionEnded.value) return
  input.value = ''
  loading.value = true
  await push('user', text)
  try {
    await streamPost('/agent/chat', { message: text, session_id: currentSessionId.value }, (c)=>push('agent', c), handleEvent)
    await loadSessions()
  } finally { loading.value = false }
}
onMounted(loadSessions)
</script>
