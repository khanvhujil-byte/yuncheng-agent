import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'
import test from 'node:test'
import ts from 'typescript'

const code = readFileSync(new URL('../src/services/planningFlow.ts', import.meta.url), 'utf8')
const module = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const { finishPlanning, completedResult } = await import(`data:text/javascript;base64,${Buffer.from(module).toString('base64')}`)

test('accepted job that times out never starts another generation', async () => {
  let duplicate = 0
  const result = await finishPlanning(async () => 'poll', async () => null, async () => { duplicate++; return { success: true } })
  assert.equal(result.kind, 'waiting'); assert.equal(duplicate, 0)
})
test('uncertain submission and polling errors propagate without duplicate calls', async () => {
  let duplicate = 0
  for (const start of [async () => { throw new Error('timeout') }, async () => 'poll']) {
    await assert.rejects(() => finishPlanning(start, async () => { throw new Error('job gone') }, async () => { duplicate++; return {} }))
  }
  assert.equal(duplicate, 0)
})
test('legacy route runs only when endpoint explicitly unavailable', async () => {
  const result = await finishPlanning(async () => 'unavailable', async () => assert.fail('must not poll'), async () => ({ success: false, message: 'validation failed' }))
  assert.equal(result.response.success, false)
})
test('partial and failed responses preserve the backend verdict', async () => {
  const partial = { success: false, data: { days: [] }, message: 'invented attraction' }
  assert.equal(completedResult(partial), partial)
  assert.equal((await finishPlanning(async () => 'poll', async () => partial, async () => assert.fail())).response, partial)
  const failed = { success: false, status: 'error', error_code: 'GENERATION_FAILED' }
  assert.equal(completedResult(failed), failed)
  assert.equal(completedResult({ success: false, status: 'running' }), null)
})
