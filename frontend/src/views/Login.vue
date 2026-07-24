<template>
  <div class="login-card">
    <h1>登录</h1>
    <p class="muted">请输入账号密码进入自动补卡系统。</p>
    <form @submit.prevent="submit">
      <label>用户名<input v-model="username" autocomplete="username" /></label>
      <label>密码<input v-model="password" type="password" autocomplete="current-password" /></label>
      <button :disabled="loading">{{ loading ? '登录中...' : '登录' }}</button>
      <p v-if="error" class="error">{{ error }}</p>
    </form>
  </div>
</template>
<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
const username = ref(''); const password = ref(''); const loading = ref(false); const error = ref('')
const auth = useAuthStore(); const router = useRouter()
async function submit(){
  loading.value = true; error.value = ''
  try { await auth.login(username.value, password.value); router.push('/repair') }
  catch(e){ error.value = e.response?.data?.detail || '登录失败' }
  finally { loading.value = false }
}
</script>
