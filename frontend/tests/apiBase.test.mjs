import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'
import test from 'node:test'
import ts from 'typescript'

const code = readFileSync(new URL('../src/services/apiBase.ts', import.meta.url), 'utf8')
const module = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const { API_BASE_URL, normalizeApiBaseUrl } = await import(`data:text/javascript;base64,${Buffer.from(module).toString('base64')}`)

test('unconfigured browser API stays on the origin used by phones and nginx', () => {
  assert.equal(API_BASE_URL, '')
  const phoneOrigin = 'http://192.168.1.8:5173'
  assert.equal(new URL(`${API_BASE_URL}/api/travel/status`, phoneOrigin).origin, phoneOrigin)
  assert.equal(new URL(`${API_BASE_URL}/api/dialog/conversations`, 'https://travel.example.com').origin, 'https://travel.example.com')
})

test('empty deployment configuration uses the reverse proxy', () => {
  for (const value of [undefined, '', '   ']) assert.equal(normalizeApiBaseUrl(value), '')
})

test('explicit API origin works with axios and streaming URL concatenation', () => {
  const origin = normalizeApiBaseUrl(' https://api.example.com/// ')
  assert.equal(origin, 'https://api.example.com')
  assert.equal(`${origin}/api/trip/plan`, 'https://api.example.com/api/trip/plan')
})
