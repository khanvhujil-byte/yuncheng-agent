import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'
import test from 'node:test'
import ts from 'typescript'
const source = readFileSync(new URL('../src/utils/tickets.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2022 } }).outputText
const { ticketText, ticketReason, ticketUnverified } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

test('reserved budget remains visibly unverified, including historical plans', () => {
  const spot = { ticket_price:100, ticket_price_status:'estimated', ticket_price_source:'static_estimate', ticket_price_note:'[WEB_QUERY_BUDGET] timeout' }
  assert.match(ticketText(spot), /票价待核实.*预算预留 ¥100/)
  assert.equal(ticketUnverified(spot), true)
  assert.match(ticketReason(spot), /查询时间或次数/)
  assert.doesNotMatch(ticketReason(spot), /WEB_QUERY/)
})

test('zero, policy reference, ordinary access and unknown are separate', () => {
  assert.match(ticketText({ticket_price:0,ticket_price_status:'known',ticket_price_source:'imported'}), /免费/)
  assert.match(ticketText({ticket_price:30,ticket_price_status:'estimated',ticket_price_source:'imported'}), /¥30.*公开政策参考/)
  assert.match(ticketText({ticket_price:0,ticket_price_status:'estimated',ticket_price_source:'venue_policy'}), /不设门票预算/)
  assert.equal(ticketText({ticket_price:null,ticket_price_status:'unknown'}),'门票待核实')
  assert.match(ticketReason({ticket_price:null,ticket_price_note:'[WEB_EVIDENCE_UNRESOLVED] VENUE_OR_CITY_MISMATCH'}), /属于这个景点/)
})

test('model daily price is numeric but visibly unverified', () => {
  for (const amount of [0,30,220]) {
    const spot = {ticket_price:amount,ticket_price_status:'estimated',ticket_price_source:'llm_estimated',ticket_price_note:'日常成人门票模型估算，未核验官方价格'}
    assert.match(ticketText(spot), /日常门票参考 ¥\d+ · 模型估算/)
    assert.equal(ticketUnverified(spot),true)
    assert.match(ticketReason(spot), /未核验/)
  }
})
