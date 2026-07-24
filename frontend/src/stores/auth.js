import { defineStore } from 'pinia'
import { api } from '../api/client'

export const useAuthStore = defineStore('auth', {
  state: () => ({ token: localStorage.getItem('token') || '', user: null }),
  actions: {
    async login(username, password) {
      const { data } = await api.post('/auth/login', { username, password })
      this.token = data.access_token
      localStorage.setItem('token', this.token)
      await this.fetchMe()
    },
    async fetchMe() {
      if (!this.token) return
      const { data } = await api.get('/users/me')
      this.user = data
    },
    logout() {
      this.token = ''
      this.user = null
      localStorage.removeItem('token')
    }
  }
})
