<template>
  <section class="panel">
    <h2>领导审核</h2>
    <button @click="load">刷新</button>
    <div v-if="items.length===0" class="muted">暂无待审核补卡单。</div>
    <div class="approval" v-for="item in items" :key="item.id">
      <h3>审核单 #{{ item.id }}：{{ item.applicant_name }}</h3>
      <p>{{ item.work_date }} {{ label(item.punch_type) }}，原因：{{ item.reason || '未填写' }}</p>
      <textarea v-model="comments[item.id]" placeholder="审核意见"></textarea>
      <div><button @click="review(item.id, true)">通过并自动补卡</button><button class="danger" @click="review(item.id, false)">拒绝</button></div>
    </div>
  </section>
</template>
<script setup>
import { onMounted, reactive, ref } from 'vue'
import { api } from '../api/client'
const items = ref([]); const comments = reactive({})
function label(t){ return t === 'ON_DUTY' ? '上班卡' : '下班卡' }
async function load(){ items.value = (await api.get('/approvals')).data }
async function review(id, approved){ await api.post(`/approvals/${id}/review`, { approved, comment: comments[id] || '' }); await load() }
onMounted(load)
</script>
