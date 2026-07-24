<template>
  <div>
    <header v-if="auth.token" class="topbar">
      <div class="brand">自动补卡 Agent</div>
      <nav>
        <RouterLink to="/repair">补卡聊天</RouterLink>
        <RouterLink to="/missing">忘记打卡录入</RouterLink>
        <RouterLink to="/approvals">领导审核</RouterLink>
      </nav>
      <div class="user">{{ auth.user?.full_name }} <button @click="logout">退出</button></div>
    </header>
    <main class="container"><RouterView /></main>
  </div>
</template>
<script setup>
import { useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
const auth = useAuthStore(); const router = useRouter()
function logout(){ auth.logout(); router.push('/login') }
</script>
