import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'


for (const configPath of ['docker/nginx-gateway.conf', 'docker/nginx.conf']) {
  test(`${configPath} allows long-running evaluation requests`, () => {
    const config = readFileSync(configPath, 'utf8')
    assert.match(config, /proxy_send_timeout\s+300s;/)
    assert.match(config, /proxy_read_timeout\s+300s;/)
  })
}
