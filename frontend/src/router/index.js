import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import Login from '../views/Login.vue'
import MissingPunch from '../views/MissingPunch.vue'
import RepairChat from '../views/RepairChat.vue'
import Approvals from '../views/Approvals.vue'

const routes = [
  { path: '/login', component: Login, meta: { public: true } },
  { path: '/', redirect: '/repair' },
  { path: '/missing', component: MissingPunch },
  { path: '/repair', component: RepairChat },
  { path: '/approvals', component: Approvals }
]

const router = createRouter({ history: createWebHistory(), routes })
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (auth.token && !auth.user) {
    try { await auth.fetchMe() } catch { auth.logout() }
  }
  if (!to.meta.public && !auth.token) return '/login'
})
export default router
