import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'
import test from 'node:test'
import ts from 'typescript'
const code = readFileSync(new URL('../src/utils/itinerary.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2022 } }).outputText
const { dayStops, routeText, preparePlanForDisplay } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const meal = type => ({ name: '同一家餐厅', restaurant_id: 'R1', type })
const spot = (poi_id, name = '同名景点') => ({ name, poi_id })
const leg = (from_name, to_name, to_id, duration_minutes = 10) => ({ from_name, to_name, to_id, duration_minutes, status: 'known', mode: 'walking' })

test('same restaurant reused for breakfast lunch dinner receives the correct occurrence route', () => {
  const day = { attractions: [spot('S1')], meals: ['breakfast','lunch','dinner'].map(meal),
    schedule: [{kind:'meal',reference_id:'R1',meal_type:'breakfast'}, {kind:'attraction',reference_id:'S1'}, {kind:'meal',reference_id:'R1',meal_type:'lunch'}, {kind:'meal',reference_id:'R1',meal_type:'dinner'}],
    route_legs: [leg('酒店','同一家餐厅','R1',1), leg('同一家餐厅','同名景点','S1',2), leg('同名景点','同一家餐厅','R1',3), leg('同一家餐厅','同一家餐厅','R1',0), leg('同一家餐厅','酒店','H1',5)] }
  const stops = dayStops(day, {name:'酒店',poi_id:'H1'})
  assert.deepEqual(stops.map(s=>s.incoming.duration_minutes), [1,2,3,0])
  assert.deepEqual(stops.filter(s=>s.kind==='meal').map(s=>s.place.type), ['breakfast','lunch','dinner'])
  assert.equal(stops[3].returnToHotel.duration_minutes, 5)
})

test('same-name attractions use exact IDs and a missing route does not borrow another attraction route', () => {
  const day = { attractions: [spot('A'),spot('B')], meals: [], schedule: [{kind:'attraction',reference_id:'B'}, {kind:'attraction',reference_id:'A'}], route_legs:[leg('酒店','同名景点','B',17)] }
  const stops = dayStops(day)
  assert.deepEqual(stops.map(s=>s.place.poi_id), ['B','A'])
  assert.equal(stops[0].incoming.duration_minutes,17)
  assert.equal(stops[1].incoming,undefined)
})

test('legacy plans preserve all stops, distinct repeated meals, and do not invent routes', () => {
  const stops = dayStops({attractions:[spot('A'),spot('B')],meals:['breakfast','lunch','dinner'].map(meal)})
  assert.equal(stops.length,5)
  assert.equal(new Set(stops.map(s=>s.key)).size,5)
  assert.ok(stops.every(s=>!s.incoming))
})

test('unknown and estimated routes retain uncertainty instead of showing zero as measured', () => {
  assert.match(routeText({...leg('A','B','B',0),status:'unknown'}),/耗时待核实/)
  assert.doesNotMatch(routeText({...leg('A','B','B',0),status:'unknown'}),/0 分钟/)
  assert.match(routeText({...leg('A','B','B',20),status:'estimated',distance_meters:2200}),/约 20 分钟 · 2.2 公里/)
})

function oldPlan() {
  return {days:[{day_index:0,attractions:[{name:'未知门票',ticket_price:null,ticket_price_status:'unknown'}, {name:'真免费',ticket_price:0,ticket_price_status:'known'}],meals:[],day_total_cost:40}],
    budget:{total:40,total_attractions:0,estimated_total:40,known_total:0,limit:100,unknown_items:['未知门票','同名餐厅'],items:[{category:'attraction',name:'未知门票',day_index:0,amount:null,status:'unknown',source:'unknown'}, {category:'attraction',name:'真免费',day_index:0,amount:0,status:'known',source:'imported'}, {category:'meal',name:'同名餐厅',amount:null,status:'unknown',source:'unknown'}]}}
}
test('historical display preserves backend amounts unknowns and verdict without inventing a reserve', () => {
  const original=oldPlan(), before=JSON.stringify(original)
  const updated=preparePlanForDisplay(original)
  assert.equal(JSON.stringify(original),before)
  assert.equal(updated.budget.total,40)
  assert.equal(updated.budget.total_attractions,0)
  assert.equal(updated.days[0].day_total_cost,40)
  assert.equal(updated.budget.limit_status,undefined)
  assert.deepEqual(updated.budget.unknown_items,['未知门票','同名餐厅'])
  assert.equal(updated.days[0].attractions[1].ticket_price,0)
  assert.deepEqual(preparePlanForDisplay(updated),updated)
})
test('display retains same-named unknown entries and does not recalculate budget', () => {
  const original=oldPlan()
  original.budget.unknown_items=['未知门票','未知门票']
  assert.deepEqual(preparePlanForDisplay(original).budget.unknown_items,['未知门票','未知门票'])
})
