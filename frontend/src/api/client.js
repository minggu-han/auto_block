import axios from 'axios'

export const api = axios.create({ baseURL: '/api' })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

export async function streamPost(url, body, onMessage, onEvent) {
  const token = localStorage.getItem('token')
  const res = await fetch(`/api${url}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    body: JSON.stringify(body || {})
  })
  if (!res.ok || !res.body) throw new Error(await res.text())
  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split('\n\n')
    buffer = events.pop() || ''
    for (const raw of events) {
      const eventLine = raw.split('\n').find((l) => l.startsWith('event: '))
      const dataLine = raw.split('\n').find((l) => l.startsWith('data: '))
      if (!dataLine) continue
      const eventName = eventLine ? eventLine.slice(7) : 'message'
      const data = JSON.parse(dataLine.slice(6))
      if (data.type === 'text') onMessage?.(data.content, data)
      onEvent?.(eventName, data)
    }
  }
}
