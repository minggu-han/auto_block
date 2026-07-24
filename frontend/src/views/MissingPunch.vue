<template>
  <section class="panel">
    <h2>忘记打卡录入</h2>
    <form class="grid-form" @submit.prevent="create">
      <label>日期（yyyy-MM-dd）<input v-model="form.work_date" type="date" required /></label>
      <label>忘记类型
        <select v-model="form.punch_type"><option value="ON_DUTY">上班卡</option><option value="OFF_DUTY">下班卡</option></select>
      </label>
      <button>添加</button>
    </form>
    <h3>我的打卡记录</h3>
    <table><thead><tr><th>ID</th><th>日期</th><th>类型</th><th>状态</th></tr></thead>
      <tbody><tr v-for="item in items" :key="item.id"><td>{{ item.id }}</td><td>{{ item.work_date }}</td><td>{{ label(item.punch_type) }}</td><td>{{ item.status }}</td></tr></tbody>
    </table>
  </section>
</template>
<script setup>
import { onMounted, reactive, ref } from 'vue'
import { api } from '../api/client'
const items = ref([])
const form = reactive({ work_date: new Date().toISOString().slice(0,10), punch_type: 'ON_DUTY' })
function label(t){ return t === 'ON_DUTY' ? '上班卡' : '下班卡' }
async function load(){ items.value = (await api.get('/missing-punches')).data }
async function create(){ await api.post('/missing-punches', form); await load() }
onMounted(load)
</script>
