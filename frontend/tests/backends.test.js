import assert from 'node:assert/strict'
import { after, before, beforeEach, test } from 'node:test'
import { createServer } from 'vite'

let server
let api
let saved = '{}'
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

before(async () => {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { getItem: () => saved, setItem: (_key, value) => { saved = value } }
  })
  server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  api = await server.ssrLoadModule('/src/lib/backends.js')
})

beforeEach(() => { saved = '{}' })

after(async () => {
  await server?.close()
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
  else delete globalThis.localStorage
})

test('migrates a saved Java selection without reusing its conversation or endpoint', () => {
  saved = JSON.stringify({
    backend: 'java', userId: 'customer', conversationId: 'java-conversation',
    endpoints: { java: '/api/java', python: '/api/python' }
  })
  const settings = api.createInitialSettings()
  assert.deepEqual(settings, {
    backend: 'python', userId: 'customer', conversationId: '',
    endpoints: { python: '/api/python' }
  })
  api.saveSettings(settings)
  assert.deepEqual(api.createInitialSettings(), settings)
  assert.equal(api.backendMeta('java', settings).baseUrl, '/api/python')
})

test('preserves an existing Python user, conversation and custom endpoint', () => {
  const settings = {
    backend: 'python', userId: 'existing-user', conversationId: 'existing-conversation',
    endpoints: { python: 'http://localhost:9000/' }
  }
  saved = JSON.stringify(settings)
  assert.deepEqual(api.createInitialSettings(), settings)
  assert.equal(api.backendMeta('python', settings).baseUrl, 'http://localhost:9000')
})

test('keeps Python API routes and chat payload and response compatible', async (t) => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, options })
    return new Response(JSON.stringify({ conv_id: 'c2', response: 'hello', latency_ms: 12 }))
  })
  const settings = { ...api.createInitialSettings(), conversationId: 'c1', endpoints: { python: '/api/python' } }
  await api.requestHealth('python', settings)
  await api.requestMonitor('python', settings)
  await api.requestSkills('python', settings)
  await api.reloadSkills('python', settings)
  await api.requestKnowledgeStats('python', settings)
  await api.runEvaluation('python', settings)
  await api.requestSearch('python', settings, 'refund')
  await api.addKnowledge('python', settings, [{ title: 'policy', content: 'refund policy' }])
  await api.uploadKnowledge('python', settings, new File(['policy'], 'policy.txt'))
  await api.requestToolTrace('python', settings, 'request-1')
  const chat = await api.requestChat('python', settings, 'hello')
  assert.deepEqual(calls.map(({ url }) => url), [
    '/api/python/health', '/api/python/monitor', '/api/python/skills',
    '/api/python/skills/reload', '/api/python/knowledge/stats', '/api/python/eval/run',
    '/api/python/search?query=refund&top_k=5', '/api/python/knowledge/add',
    '/api/python/knowledge/upload', '/api/python/trace/tool/request-1', '/api/python/chat'
  ])
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), {
    message: 'hello', user_id: 'u1001', conv_id: 'c1'
  })
  assert.equal(chat.conversationId, 'c2')
  assert.equal(chat.response, 'hello')
  assert.equal(chat.latencyMs, 12)
})
