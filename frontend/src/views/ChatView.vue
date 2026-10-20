<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ArrowRight, ArrowUp, BookOpen, Check, ChevronDown, Compass, Heart, LoaderCircle, LogOut, MapPin, Plus, Route, ShieldCheck, SlidersHorizontal, Utensils, X } from 'lucide-vue-next'
import BrandLockup from '@/components/BrandLockup.vue'
import JourneyArtwork from '@/components/JourneyArtwork.vue'
import ExportPanel from '@/components/ExportPanel.vue'
import TripSummary from '@/components/TripSummary.vue'
import DayTimeline from '@/components/DayTimeline.vue'
import PlanModuleEditor from '@/components/PlanModuleEditor.vue'
import RequirementPanel from '@/components/RequirementPanel.vue'
import JourneyInputs from '@/components/JourneyInputs.vue'
import TravelSearch from '@/components/TravelSearch.vue'
import { preparePlanForDisplay } from '@/utils/itinerary'
import { DialogError, hasSession, login, request } from '@/services/dialog'
import type { ClarificationAnswer, Citation, Conversation, Document, ModuleEdit, Preference, Quality, Slots, Turn, TurnResult } from '@/services/dialog'
import type { Requirements, TripPlanResponse } from '@/types/trip'

const signedIn = ref(hasSession())
const account = reactive({ username: '', password: '' })
const current = ref<Conversation | null>(null)
const conversations = ref<{ conversation_id: string; updated_at: string }[]>([])
const preferences = ref<Preference[]>([])
const documents = ref<Document[]>([])
const versions = ref<{ run_id: string; created_at: string; result: TripPlanResponse }[]>([])
const quality = ref<Quality | null>(null)
const message = ref('')
const error = ref('')
const busy = ref(false)
const pending = ref<Turn | null>(null)
const citations = ref<Citation[]>([])
const candidate = ref<TripPlanResponse | null>(null)
const moduleEditor = ref<InstanceType<typeof PlanModuleEditor> | null>(null)
const editor = reactive({ city: '', start_date: '', travel_days: 3, transportation: '公共交通', accommodation: '经济型酒店',
  origin_city: '', intercity_mode: 'none' as 'none' | 'train' | 'flight' | 'auto', return_trip: true, adult_count: 1, room_count: 1,
  budget_limit: '', nights: '', preferences: '', must_visit: '', fixed_hotel: '', fixed_hotel_address: '', free_text_input: '' })
const pref = reactive({ key: 'transportation', value: '公共交通' })
const doc = reactive({ title: '', source: '', content: '', valid_until: '' })
const shown = computed(() => {
  const result = candidate.value?.data ? candidate.value : current.value?.edit_preview?.data ? current.value.edit_preview : current.value?.result
  return result?.data ? { ...result, data: preparePlanForDisplay(result.data) } : result
})
const ready = computed(() => !!current.value?.slots.city && !!current.value?.slots.start_date && !!current.value?.slots.travel_days && !current.value?.clarifications?.length)
const moduleRequirements = computed(() => (current.value?.slots.requirements ?? {}) as Requirements)
const authMode = ref<'login' | 'register'>('login')
const activePanel = ref<'trips' | 'preferences' | 'documents' | 'quality'>('trips')
const formOpen = ref(false)
const accountName = ref(sessionStorage.getItem('trip-account-name') || '旅行者')
const selectedDay = ref<number | null>(null)
const visibleDays = computed(() => selectedDay.value === null ? shown.value?.data?.days ?? [] : (shown.value?.data?.days ?? []).filter((_, index) => index === selectedDay.value))
const tripTitle = computed(() => current.value?.slots.city ? `${current.value.slots.city}，慢慢逛。` : '下一站，你想去哪里？')
const interestOptions = ['美食', '自然风光', '历史文化', '博物馆', '摄影', '亲子']
function toggleInterest(interest: string) {
  const values = editor.preferences.split(/[,，、]/).map(v => v.trim()).filter(Boolean)
  editor.preferences = (values.includes(interest) ? values.filter(v => v !== interest) : [...values, interest]).join('，')
}
function useSuggestion(city: string, interest: string) {
  editor.city = city; editor.preferences = interest; formOpen.value = true
  document.querySelector('.planning-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
function dateLabel(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '最近的行程' : date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

async function task(action: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await action() } catch (e) {
    error.value = e instanceof Error ? e.message : '操作失败'
    signedIn.value = hasSession()
  } finally { busy.value = false }
}
function syncEditor(slots: Slots) {
  for (const key of Object.keys(editor)) {
    const value = slots[key]
    if (key === 'travel_days') editor.travel_days = typeof value === 'number' ? value : 3
    else if (key === 'adult_count') editor.adult_count = typeof value === 'number' ? value : 1
    else if (key === 'room_count') editor.room_count = typeof value === 'number' ? value : 1
    else if (key === 'return_trip') editor.return_trip = typeof value === 'boolean' ? value : true
    else if (key === 'intercity_mode') editor.intercity_mode = (value || 'none') as typeof editor.intercity_mode
    else if (key === 'transportation') editor.transportation = String(value || '公共交通')
    else if (key === 'accommodation') editor.accommodation = String(value || '经济型酒店')
    else (editor as Record<string, unknown>)[key] = Array.isArray(value) ? value.join('，') : String(value ?? '')
  }
}
async function refresh() {
  const [cs, ps, ds] = await Promise.all([
    request<typeof conversations.value>('/conversations'), request<Preference[]>('/preferences'), request<Document[]>('/knowledge/documents'),
  ])
  conversations.value = cs; preferences.value = ps; documents.value = ds
}
async function open(id: string) {
  current.value = await request<Conversation>(`/conversations/${id}`)
  sessionStorage.setItem('trip-conversation', id)
  syncEditor(current.value.slots); candidate.value = null; citations.value = []
  selectedDay.value = null
  const raw = sessionStorage.getItem(`trip-pending:${id}`)
  pending.value = raw ? JSON.parse(raw) as Turn : null
  versions.value = await request<typeof versions.value>(`/conversations/${id}/versions`)
}
async function create() {
  const created = await request<Conversation>('/conversations', 'POST')
  await open(created.conversation_id); await refresh()
}
async function authenticate(register = false) {
  await task(async () => {
    if (register) await request('/auth/register', 'POST', account)
    await login(account.username, account.password)
    accountName.value = account.username; sessionStorage.setItem('trip-account-name', account.username)
    account.password = ''; signedIn.value = true; await refresh()
    const id = conversations.value[0]?.conversation_id
    if (id) await open(id); else await create()
  })
}
async function logout() {
  await task(async () => { await request('/auth/logout', 'POST'); sessionStorage.removeItem('trip-session'); sessionStorage.removeItem('trip-account-name'); signedIn.value = false; current.value = null })
}
async function send(action: Turn['action'], updates?: Slots, edit?: ModuleEdit, answer?: ClarificationAnswer) {
  const c = current.value
  if (!c) return
  await task(async () => {
    const command: Turn = pending.value ?? { request_id: crypto.randomUUID(), expected_revision: c.revision, action,
      ...(action === 'message' ? { message: message.value, ...(updates ? { slot_updates: updates } : {}) } : {}),
      ...(action === 'edit' && edit ? { edit } : {}) }
    if (command.action === 'answer' && !command.answer && answer) command.answer = answer
    pending.value = command
    sessionStorage.setItem(`trip-pending:${c.conversation_id}`, JSON.stringify(command))
    let response: TurnResult
    try { response = await request<TurnResult>(`/conversations/${c.conversation_id}/turns`, 'POST', command) }
    catch (e) {
      if (e instanceof DialogError && e.status >= 400 && e.status < 500) {
        pending.value = null; sessionStorage.removeItem(`trip-pending:${c.conversation_id}`)
        if (e.status === 409) await open(c.conversation_id)
      }
      throw e
    }
    pending.value = null; sessionStorage.removeItem(`trip-pending:${c.conversation_id}`)
    await open(c.conversation_id)
    candidate.value = response.candidate_result; citations.value = response.citations
    message.value = ''; await refresh()
  })
}
function saveDraft() {
  const split = (s: string) => s.split(/[,，、]/).map(v => v.trim()).filter(Boolean)
  const slots: Slots = { ...editor, preferences: split(editor.preferences), must_visit: split(editor.must_visit),
    nights: editor.nights === '' ? null : Number(editor.nights), budget_limit: editor.budget_limit === '' ? null : Number(editor.budget_limit),
    fixed_hotel: editor.fixed_hotel || null, fixed_hotel_address: editor.fixed_hotel_address || null }
  void send('message', slots)
}
async function savePreference() {
  await task(async () => {
    const value = pref.key === 'preferences' ? pref.value.split(/[,，、]/).filter(Boolean) : pref.key === 'budget_limit' ? Number(pref.value) : pref.value
    await request(`/preferences/${pref.key}`, 'PUT', { value, expected_version: preferences.value.find(p => p.key === pref.key)?.version ?? 0, source: '用户在偏好面板明确保存' })
    await refresh()
  })
}
onMounted(() => { if (signedIn.value) void task(async () => { await refresh(); const id = sessionStorage.getItem('trip-conversation'); if (id && conversations.value.some(c => c.conversation_id === id)) await open(id); else if (conversations.value[0]) await open(conversations.value[0].conversation_id); else await create() }) })
</script>

<template>
  <div class="yuncheng-app">
    <header class="topbar">
      <router-link to="/chat" class="brand" aria-label="云程，回到旅行规划"><BrandLockup /></router-link>
      <nav class="top-nav" aria-label="主导航"><router-link to="/chat" class="active">旅行工作台</router-link><router-link to="/">快速规划</router-link><router-link to="/community">旅行社区 <ArrowRight :size="13" /></router-link></nav>
      <div class="topbar-end"><span class="product-note">把时间，留给喜欢的事。</span><span v-if="signedIn" class="account-avatar" :title="accountName">{{ accountName.slice(0, 1).toUpperCase() }}</span><a v-else href="#welcome" class="nav-start">开始一段旅行 <ArrowRight :size="14" /></a></div>
    </header>

    <main v-if="!signedIn" id="welcome" class="welcome">
      <section class="welcome-story">

        <h1>去喜欢的地方，<br>过几天<span>慢生活。</span><svg viewBox="0 0 230 15" aria-hidden="true"><path d="M3 10Q107-4 226 7" stroke="currentColor" stroke-width="3" fill="none" stroke-linecap="round"/></svg></h1>
        <p class="welcome-copy">沿着街巷找一家小馆，在湖边多坐一会儿。<br>把想去的地方告诉云程，剩下的，一起慢慢安排。</p>
        <JourneyArtwork />
        <div class="welcome-features"><span><Utensils :size="17" /> 认真吃好每一餐</span><span><Route :size="17" /> 少折返，多感受</span><span><SlidersHorizontal :size="17" /> 行程随心微调</span></div>
      </section>
      <section class="auth-card" aria-labelledby="auth-title">
        <span class="small-compass"><Compass :size="25" :stroke-width="1.4" /></span>
        <h2 id="auth-title">{{ authMode === 'login' ? '欢迎回来，旅行者。' : '你的下一站，从这里开始。' }}</h2><p class="auth-description">保存每一次出发，也记住你的旅行偏好。</p>
        <div class="auth-tabs" role="tablist" aria-label="账号操作"><button role="tab" :aria-selected="authMode === 'login'" :class="{ selected: authMode === 'login' }" @click="authMode = 'login'">登录</button><button role="tab" :aria-selected="authMode === 'register'" :class="{ selected: authMode === 'register' }" @click="authMode = 'register'">创建账号</button></div>
        <form @submit.prevent="authenticate(authMode === 'register')">
          <label class="field">用户名<input v-model="account.username" autocomplete="username" placeholder="3～32 位字母、数字或下划线" minlength="3" maxlength="32" required :disabled="busy"></label>
          <label class="field">密码<input v-model="account.password" type="password" :autocomplete="authMode === 'register' ? 'new-password' : 'current-password'" placeholder="至少 10 个字符" minlength="10" required :disabled="busy"></label>
          <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
          <button class="button primary auth-submit" :disabled="busy" type="submit"><LoaderCircle v-if="busy" :size="17" class="spin" /><span>{{ busy ? '正在准备你的空间…' : authMode === 'login' ? '开启我的旅行' : '创建账号，开始旅程' }}</span><ArrowRight v-if="!busy" :size="17" /></button>
        </form>
        <div class="auth-footnote"><ShieldCheck :size="15" /><span>你的会话、偏好与旅行资料，保存在自己的账号中。</span></div>
        <div class="auth-card-bottom"><span>每一份行程，都可以慢慢打磨。</span><span>✳</span></div>
      </section>
      <footer class="welcome-footer"><span>云程 YUNCHENG · 为每一种旅行节奏</span><span>好好规划，自在出发。</span></footer>
    </main>

    <div v-else class="workbench">
      <aside class="sidebar">
        <button class="button primary new-trip" :disabled="busy || !!pending" @click="activePanel = 'trips'; task(create)"><Plus :size="17" /> 新建一段旅程 <span>＋</span></button>
        <div class="sidebar-label">我的空间 <span>YOUR SPACE</span></div>
        <nav class="side-nav" aria-label="旅行空间"><button :class="{ selected: activePanel === 'trips' }" :aria-pressed="activePanel === 'trips'" @click="activePanel = 'trips'"><Compass :size="18" /> 我的行程 <span>{{ conversations.length }}</span></button><button :class="{ selected: activePanel === 'preferences' }" :aria-pressed="activePanel === 'preferences'" @click="activePanel = 'preferences'"><Heart :size="18" /> 旅行偏好</button><button :class="{ selected: activePanel === 'documents' }" :aria-pressed="activePanel === 'documents'" @click="activePanel = 'documents'"><BookOpen :size="18" /> 旅行资料 <span v-if="documents.length">{{ documents.length }}</span></button></nav>
        <div class="sidebar-label trip-list-label">最近的旅程 <span>{{ String(conversations.length).padStart(2, '0') }}</span></div>
        <div class="trip-list"><button v-for="(c, index) in conversations" :key="c.conversation_id" :class="{ selected: c.conversation_id === current?.conversation_id }" :disabled="busy || !!pending" @click="activePanel = 'trips'; task(() => open(c.conversation_id))"><span class="trip-list-icon"><MapPin :size="15" /></span><span><strong>{{ c.conversation_id === current?.conversation_id && current.slots.city ? `${current.slots.city}旅行计划` : `旅行计划 ${String(conversations.length - index).padStart(2, '0')}` }}</strong><small>{{ dateLabel(c.updated_at) }} · {{ c.conversation_id === current?.conversation_id ? '正在编辑' : '已保存' }}</small></span><span v-if="c.conversation_id === current?.conversation_id" class="live-dot"></span></button></div>
        <div class="mobile-trip-controls"><label class="sr-only" for="mobile-trip-select">切换已保存的旅程</label><select id="mobile-trip-select" :value="current?.conversation_id" :disabled="busy || !!pending" @change="activePanel = 'trips'; task(() => open(($event.target as HTMLSelectElement).value))"><option v-for="(c, index) in conversations" :key="c.conversation_id" :value="c.conversation_id">{{ c.conversation_id === current?.conversation_id && current.slots.city ? `${current.slots.city}旅行计划` : `旅行计划 ${conversations.length - index}` }} · {{ dateLabel(c.updated_at) }}</option></select><button class="text-button" aria-label="查看运行状态" @click="activePanel = 'quality'"><ShieldCheck :size="17" /></button><button class="text-button" :disabled="busy" aria-label="退出登录" @click="logout"><LogOut :size="16" /></button></div>
        <div class="sidebar-bottom"><div class="slow-note"><span>TRAVEL AT YOUR OWN PACE</span><p>不必赶路，<br>好风景值得多停一会儿。</p><Compass :size="42" :stroke-width=".8" /></div><button class="side-utility" :class="{ selected: activePanel === 'quality' }" @click="activePanel = 'quality'"><ShieldCheck :size="16" /> 运行状态</button><button class="side-utility" :disabled="busy" @click="logout"><LogOut :size="16" /> 退出登录</button></div>
      </aside>

      <main class="workspace">
        <div v-if="error" class="error-banner" role="alert">{{ error }}</div>
        <template v-if="activePanel === 'trips'">
          <div class="workspace-heading"><div><h1>{{ tripTitle }}</h1><p>把想法交给云程，把期待留给出发。</p></div><div class="step-label"><span :class="{ complete: ready }">01 <Check v-if="ready" :size="11" /></span> 聊聊想法 <i></i><span :class="{ complete: current?.result?.success }">02</span> 安排行程</div></div>
          <div class="planning-layout">
            <section v-if="current" class="planning-panel">
              <div class="panel-heading"><span class="panel-symbol"><SlidersHorizontal :size="18" /></span><div><h2>这次，想怎么旅行？</h2><p>从一个目的地，或一个小小的想法开始。</p></div></div>
              <div v-if="current.history.length" class="chat-history" aria-live="polite"><article v-for="h in current.history" :key="h.revision"><p v-if="h.message" class="user-message">{{ h.message }}</p><div class="assistant-message"><Compass :size="17" /><p>{{ h.reply }}</p></div></article></div>
              <section v-if="current.clarifications?.length" class="clarification-panel" aria-live="polite">
                <h3>先确认几个选择</h3><p>选好后再生成，也可以在下方直接补充说明。</p>
                <article v-for="question in current.clarifications" :key="question.question_id">
                  <strong>{{ question.question }}</strong>
                  <div class="question-options"><button v-for="(option, index) in question.options" :key="index" :disabled="busy || !!pending" @click="send('answer', undefined, undefined, { question_id: question.question_id, option_index: index })">{{ option.label }}</button></div>
                </article>
              </section>
              <div class="composer"><label class="sr-only" for="trip-message">告诉云程你的旅行想法</label><textarea id="trip-message" v-model="message" :disabled="busy || !!pending" rows="3" placeholder="想找一家地道小馆，也想在西湖边发发呆……" @keydown.ctrl.enter="send('message')"></textarea><div class="composer-footer"><span>说说你的想法 · Ctrl + Enter 发送</span><button class="send-button" :disabled="busy || (!message.trim() && !pending)" :aria-label="pending ? '重试并确认上次操作' : '发送旅行想法'" @click="send('message')"><LoaderCircle v-if="busy" :size="17" class="spin" /><ArrowUp v-else :size="19" /></button></div></div>
              <div class="or-divider"><span>也可以，直接告诉我</span></div>
              <details class="travel-form" :open="formOpen || !ready"><summary>完善旅行信息 <ChevronDown :size="15" /></summary>
                <fieldset :disabled="busy || !!pending">
                  <JourneyInputs :model="editor" :disabled="busy || !!pending" />
                  <TravelSearch :city="editor.city" :origin="editor.origin_city" :start-date="editor.start_date" :days="editor.travel_days" :disabled="busy || !!pending" />
                  <div class="fields"><label class="field">目的地<div class="input-icon"><MapPin :size="15" /><input v-model="editor.city" placeholder="你想去的城市"></div></label><label class="field">出发日期<input v-model="editor.start_date" type="date"></label><label class="field">旅行天数<div class="number-field"><input v-model.number="editor.travel_days" type="number" min="1" max="30"><span>天</span></div></label><label class="field">市内交通<select v-model="editor.transportation"><option>公共交通</option><option>自驾</option><option>步行</option><option>混合</option></select></label><label class="field">住宿偏好<select v-model="editor.accommodation"><option>经济型酒店</option><option>舒适型酒店</option><option>豪华酒店</option><option>民宿</option></select></label><label class="field">预算上限 <span class="optional">选填</span><div class="number-field"><input v-model="editor.budget_limit" type="number" min="1" placeholder="暂时不限"><span>元</span></div></label></div>
                  <div class="interest-field"><span>你喜欢的旅行方式</span><div class="interest-chips"><button v-for="interest in interestOptions" :key="interest" type="button" :aria-pressed="editor.preferences.split(/[,，、]/).includes(interest)" :class="{ selected: editor.preferences.split(/[,，、]/).includes(interest) }" @click="toggleInterest(interest)"><Utensils v-if="interest === '美食'" :size="12" />{{ interest }}</button></div><label class="sr-only" for="other-interests">兴趣偏好，可自定义</label><input id="other-interests" v-model="editor.preferences" class="custom-interest" placeholder="或添加其他兴趣，用逗号分隔"></div>
                  <details class="extra-settings"><summary><Plus :size="13" /> 必去景点、已订酒店与其他要求</summary><div class="fields"><label class="field">必去景点<input v-model="editor.must_visit" placeholder="多个景点用逗号分隔"></label><label class="field">住宿晚数<input v-model="editor.nights" type="number" min="0" :placeholder="`默认 ${Math.max(0, editor.travel_days - 1)} 晚`"></label><label class="field">已订酒店<input v-model="editor.fixed_hotel" placeholder="已预订的酒店名称"></label><label class="field">酒店地址<input v-model="editor.fixed_hotel_address" placeholder="帮助找到正确的分店"></label></div><label class="field">额外要求<textarea v-model="editor.free_text_input" rows="2" placeholder="例如：希望午后多留一些休息时间"></textarea></label></details>
                  <button class="button secondary save-requirements" type="button" @click="saveDraft">保存这次旅行要求 <Check :size="15" /></button>
                </fieldset>
              </details>
              <RequirementPanel :value="moduleRequirements" :disabled="busy || !!pending" @save="value => send('message', { requirements: value })" />
              <div v-if="ready" class="ready-summary"><span class="live-dot"></span><span>{{ current.slots.city }} · {{ current.slots.start_date }} · {{ current.slots.travel_days }} 天</span><span class="saved-label">要求已保存</span></div>
              <div v-if="current.pending_edit" class="notice"><SlidersHorizontal :size="17" /><div><strong>局部修改，等待你确认</strong><p>{{ current.pending_edit.summary || '检查右侧预览，确认后保存为新版本。' }}</p><p>{{ current.edit_preview?.message }}</p></div></div>
              <button class="button primary generate-button" :disabled="busy || !!pending || !ready" @click="send('confirm')"><LoaderCircle v-if="busy" :size="16" class="spin" /><span>{{ busy ? '正在用心安排，请稍等…' : current.pending_edit ? '确认这次局部修改' : '确认要求，安排我的旅程' }}</span><ArrowRight v-if="!busy" :size="17" /></button>
              <button v-if="current.pending_edit || current.clarifications?.length || (current.result && ready)" class="text-button discard-button" :disabled="busy || !!pending" @click="send('reject')">撤销未确认的修改</button>
              <p class="planning-hint"><ShieldCheck :size="13" /> 修改前的行程会保留，你始终可以回来查看。</p>
              <div v-if="pending" class="notice"><LoaderCircle :size="16" class="spin" /><div>操作结果尚未确认，刷新后也可以继续。<button class="text-button" :disabled="busy" @click="send('message')">重试并确认上次操作 <ArrowRight :size="12" /></button></div></div>
              <p v-if="candidate && !candidate.success && candidate.error_code !== 'EDIT_PENDING'" class="error-banner">{{ candidate.message }}。原行程仍然保留。</p>
              <blockquote v-for="c in citations" :key="c.chunk_id"><strong>{{ c.title }}</strong><p>{{ c.content }}</p><small>出处：{{ c.source }}</small></blockquote>
            </section>

            <section class="itinerary-canvas" :class="{ 'has-plan': shown?.data }" aria-label="行程预览">
              <template v-if="shown?.data">
                <div class="canvas-heading"><span><span class="live-dot"></span> {{ current?.pending_edit ? '修改预览' : '我的旅行手册' }}</span><span>{{ shown.data.days.length }} DAYS · {{ shown.data.city }}</span></div>
                <p v-if="current?.pending_edit" class="notice">这是一份修改预览，确认后才会替换已保存的行程。</p>
                <TripSummary :plan="shown.data" :preferences="[]" hide-actions :editable="!candidate && !current?.edit_preview && !busy && !pending && !current?.pending_edit && !current?.clarifications?.length" @edit-stay="index => moduleEditor?.openStay(index)" />
                <div class="day-navigation" aria-label="选择行程日期"><button :class="{ selected: selectedDay === null }" :aria-pressed="selectedDay === null" @click="selectedDay = null">完整行程</button><button v-for="(day, index) in shown.data.days" :key="day.date" :class="{ selected: selectedDay === index }" :aria-pressed="selectedDay === index" @click="selectedDay = index">DAY {{ index + 1 }}</button></div>
                <DayTimeline :days="visibleDays" :sources="shown.data.sources" :hotel="shown.data.hotel" />
                <p v-if="current?.result?.data && !current.result.success" class="notice">这份草稿尚未通过校验。可修改具体模块，全部校验通过后才会保存为正式行程。</p>
                <PlanModuleEditor v-if="current?.result?.data" ref="moduleEditor" :conversation-id="current.conversation_id" :plan="current.result.data" :disabled="busy || !!pending || !!current.pending_edit || !!current.clarifications?.length" @edit="edit => send('edit', undefined, edit)" />
                <details class="version-history export-options"><summary><BookOpen :size="15" /> 把旅行手册带走或分享 <ChevronDown :size="14" /></summary><ExportPanel :plan="shown.data" :preferences="Array.isArray(current?.slots.preferences) ? current.slots.preferences : []" /></details>
                <details v-if="versions.length" class="version-history"><summary><BookOpen :size="15" /> 旅行手册的 {{ versions.length }} 个版本 <ChevronDown :size="14" /></summary><article v-for="(v, index) in versions" :key="v.run_id"><span>版本 {{ index + 1 }}<small>{{ dateLabel(v.created_at) }} · {{ v.result.success ? '通过校验' : '尚未完成' }}</small></span><button class="text-button" @click="candidate = v.result; selectedDay = null">查看版本 <ArrowRight :size="13" /></button></article></details>
              </template>
              <template v-else>
                <div class="canvas-heading"><span><span class="live-dot"></span> 旅程，正在酝酿</span></div>
                <div class="empty-canvas"><h2>一城一味，总有值得出发的理由。</h2><JourneyArtwork /><div class="inspiration-cards"><button @click="useSuggestion('杭州', '自然风光，美食')"><span class="destination-icon"><MapPin :size="20" /></span><strong>杭州<small>湖山之间，寻一口鲜</small></strong><ArrowRight :size="15" /></button><button @click="useSuggestion('成都', '美食，历史文化')"><span class="destination-icon food"><Utensils :size="20" /></span><strong>成都<small>茶馆小坐，街头寻味</small></strong><ArrowRight :size="15" /></button><button @click="useSuggestion('北京', '历史文化，美食')"><span class="destination-icon culture"><Compass :size="20" /></span><strong>北京<small>拐进胡同，遇见日常</small></strong><ArrowRight :size="15" /></button></div><div class="canvas-bottom"><span><Utensils :size="13" /> 当地美食</span><span><Route :size="13" /> 顺路安排</span><span><SlidersHorizontal :size="13" /> 随时微调</span></div></div>
              </template>
            </section>
          </div>
        </template>

        <section v-else-if="activePanel === 'preferences'" class="utility-page"><h1>记住你喜欢的节奏。</h1><p class="utility-intro">明确保存后，偏好会用于新的旅行会话。这次聊天里的想法不会自动变成长期偏好。</p><div class="utility-card"><h2><Heart :size="20" /> 我的旅行偏好</h2><div v-if="!preferences.some(p => p.active)" class="utility-empty">还没有保存偏好。从喜欢的交通、住宿或美食开始吧。</div><ul class="saved-list"><li v-for="p in preferences.filter(p => p.active)" :key="p.key"><span><strong>{{ ({ transportation: '交通方式', accommodation: '住宿偏好', preferences: '旅行兴趣', budget_limit: '预算上限' } as Record<string, string>)[p.key] || p.key }}</strong><small>{{ Array.isArray(p.value) ? p.value.join('、') : p.value }}</small></span><button class="text-button" :disabled="busy" @click="task(async () => { await request(`/preferences/${p.key}?expected_version=${p.version}`, 'DELETE'); await refresh() })">撤回 <X :size="13" /></button></li></ul><div class="fields"><label class="field">偏好类型<select v-model="pref.key"><option value="transportation">交通方式</option><option value="accommodation">住宿偏好</option><option value="preferences">兴趣</option><option value="budget_limit">预算上限</option></select></label><label class="field">你的偏好<input v-model="pref.value" placeholder="例如：公共交通"></label></div><button class="button primary" :disabled="busy" @click="savePreference">保存我的偏好 <Check :size="15" /></button></div></section>
        <section v-else-if="activePanel === 'documents'" class="utility-page"><h1>好好收着，出发要用。</h1><p class="utility-intro">把预约说明、退改规则和旅行笔记放在这里。询问时可以查看原文和出处。</p><div class="utility-card"><h2><BookOpen :size="20" /> 添加旅行资料</h2><div class="fields"><label class="field">资料标题<input v-model="doc.title" placeholder="例如：博物馆预约须知"></label><label class="field">资料出处<input v-model="doc.source" placeholder="网页地址或文件名"></label></div><label class="field">正文<textarea v-model="doc.content" rows="6" placeholder="粘贴想保存的资料内容"></textarea></label><label class="field">有效期 <span class="optional">选填</span><input v-model="doc.valid_until" type="date"></label><button class="button primary" :disabled="busy || !doc.title || !doc.content" @click="task(async () => { await request('/knowledge/documents', 'POST', { ...doc, valid_until: doc.valid_until || null }); doc.content = ''; await refresh() })">保存这份资料 <Plus :size="15" /></button><ul class="saved-list"><li v-for="d in documents" :key="d.document_id"><span><strong>{{ d.title }}</strong><small>{{ d.source }}</small></span><button class="text-button" :disabled="busy" @click="task(async () => { await request(`/knowledge/documents/${d.document_id}`, 'DELETE'); await refresh() })">删除 <X :size="13" /></button></li></ul></div></section>
        <section v-else class="utility-page"><h1>每次安排，都有迹可循。</h1><p class="utility-intro">这里展示真实运行记录中的统计，帮助了解规划与工具的稳定性。</p><div class="utility-card"><h2><ShieldCheck :size="20" /> 运行状态</h2><button class="button secondary" :disabled="busy" @click="task(async () => { quality = await request<Quality>('/observability/quality') })">查看最新统计 <ArrowRight :size="15" /></button><div v-if="quality" class="quality-grid"><div><span>规划次数</span><strong>{{ quality.sample_runs }}</strong></div><div><span>首次工具成功率</span><strong>{{ (quality.first_success_rate * 100).toFixed(1) }}<small>%</small></strong></div><div><span>重试率</span><strong>{{ (quality.retry_rate * 100).toFixed(1) }}<small>%</small></strong></div><div><span>最终成功率</span><strong>{{ (quality.final_success_rate * 100).toFixed(1) }}<small>%</small></strong></div><p v-for="alert in quality.alerts" :key="alert.code" class="notice">{{ alert.code }}：{{ alert.action }}</p></div></div></section>
        <footer class="workspace-footer"><span>云程 YUNCHENG</span><span>认真安排每一次出发。</span></footer>
      </main>
    </div>
  </div>
</template>

<style scoped>
.yuncheng-app {
  --ink: var(--yc-ink);
  --muted: var(--yc-muted);
  --accent: var(--yc-primary);
  --line: var(--yc-border);
  --page-background: var(--yc-page);
  min-height: 100vh;
  background: var(--page-background);
  color: var(--ink);
  font-family: Inter,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
}

button,a,input,textarea,select {
  font: inherit;
}

button,a {
  -webkit-tap-highlight-color: transparent;
}

button {
  cursor: pointer;
}

button:disabled {
  opacity: .45;
  cursor: not-allowed;
}

button:focus-visible,a:focus-visible,summary:focus-visible {
  outline: 3px solid var(--yc-muted);
  outline-offset: 4px;
}

a {
  color: inherit;
  text-decoration: none;
}

h1,h2,h3,p {
  margin: 0;
}

button {
  transition: background .18s,color .18s,box-shadow .18s,transform .18s;
}

button:not(:disabled):active {
  transform: translateY(1px);
}

.topbar {
  height: 82px;
  padding: 0 4%;
  display: flex;
  align-items: center;
  gap: 44px;
  border-bottom: 1px solid var(--line);
  background: var(--yc-surface);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.brand-icon {
  width: 40px;
  height: 40px;
  background: var(--accent);
  color: #fff;
  border-radius: 13px;
  display: grid;
  place-items: center;
}





.top-nav {
  display: flex;
  align-items: center;
  gap: 31px;
  height: 100%;
  font-size: 13px;
  color: var(--yc-muted);
}

.top-nav a {
  position: relative;
  display: flex;
  gap: 5px;
  align-items: center;
  height: 100%;
}

.top-nav a.active {
  color: var(--accent);
  font-weight: 600;
}

.top-nav a.active:after {
  content: '';
  position: absolute;
  height: 3px;
  left: 12px;
  right: 12px;
  bottom: 0;
  background: var(--accent);
  border-radius: 2px;
}

.top-nav a:hover {
  color: var(--accent);
}

.topbar-end {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 25px;
}

.product-note {
  font-size: 11px;
  color: var(--yc-muted);
}

.nav-start {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  font-weight: 600;
}

.account-avatar {
  width: 34px;
  height: 34px;
  border: 1px solid var(--yc-border);
  background: var(--yc-soft);
  display: grid;
  place-items: center;
  border-radius: 50%;
  font-size: 13px;
}

.eyebrow {
  font-size: 9px;
  letter-spacing: 2.4px;
  color: var(--yc-muted);
  font-weight: 600;
}

.welcome {
  display: grid;
  grid-template-columns: minmax(0,1.4fr) minmax(350px,.85fr);
  column-gap: 9%;
  align-items: center;
  max-width: 1260px;
  margin: auto;
  padding: 64px 45px 24px;
}

.welcome-story>.eyebrow {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 9px;
  letter-spacing: 2px;
}

.eyebrow>span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #b9935c;
}

.welcome-story h1 {
  position: relative;
  font-family: 'Noto Serif SC','STZhongsong','Songti SC','SimSun',serif;
  font-size: clamp(38px,4.1vw,59px);
  line-height: 1.46;
  letter-spacing: 1px;
  font-weight: 500;
  margin: 22px 0 20px;
}

.welcome-story h1>span {
  color: var(--yc-text);
}

.welcome-story h1>svg {
  position: absolute;
  width: 208px;
  height: 16px;
  bottom: -3px;
  left: 181px;
  color: var(--yc-muted);
}

.welcome-copy {
  font-size: 13px;
  color: var(--yc-muted);
  line-height: 2;
  margin-bottom: 24px;
}

.welcome-story>.journey-art {
  max-width: 580px;
}

.welcome-features {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 26px;
  font-size: 11px;
  color: var(--yc-muted);
}

.welcome-features>span {
  display: flex;
  gap: 7px;
  align-items: center;
}

.auth-card {
  background: var(--yc-surface);
  border: 1px solid var(--yc-border);
  border-radius: 21px;
  padding: 38px 35px 0;
  box-shadow: 0 16px 50px #243b5510;
  align-self: center;
  margin-bottom: 15px;
}

.small-compass {
  width: 47px;
  height: 47px;
  display: grid;
  place-items: center;
  border-radius: 15px;
  border: 1px solid var(--yc-border);
  background: var(--yc-page);
  color: var(--yc-text);
  margin-bottom: 27px;
}

.auth-card>.eyebrow {
  font-size: 8px;
  letter-spacing: 2px;
}

.auth-card h2 {
  font-size: 23px;
  font-weight: 600;
  letter-spacing: -.8px;
  margin: 12px 0;
}

.auth-description {
  font-size: 11px;
  line-height: 1.9;
  color: var(--muted);
}

.auth-tabs {
  display: flex;
  border-bottom: 1px solid var(--line);
  margin: 29px 0 24px;
  gap: 28px;
}

.auth-tabs button {
  border: 0;
  background: none;
  padding: 0 3px 13px;
  color: var(--yc-muted);
  font-size: 12px;
  position: relative;
}

.auth-tabs .selected {
  color: var(--accent);
  font-weight: 600;
}

.auth-tabs .selected:after {
  content: '';
  height: 2px;
  background: var(--accent);
  position: absolute;
  bottom: -1px;
  left: 0;
  right: 0;
}

.field {
  display: block;
  font-size: 11px;
  font-weight: 500;
  color: var(--yc-text);
  line-height: 1.5;
}

.field input,.field select,.field textarea,.custom-interest {
  display: block;
  background: var(--yc-surface);
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  border: 1px solid var(--yc-border);
  border-radius: 9px;
  padding: 11px 12px;
  color: var(--yc-primary);
  font-size: 12px;
  outline: none;
  margin-top: 8px;
  transition: border-color .2s,box-shadow .2s;
}

.field input:focus,.field select:focus,.field textarea:focus,.custom-interest:focus {
  border-color: var(--yc-muted);
  box-shadow: 0 0 0 3px var(--yc-soft);
}

.field input::placeholder,.field textarea::placeholder,.custom-interest::placeholder {
  color: var(--yc-muted);
  font-size: 11px;
}

.auth-card .field {
  margin-bottom: 21px;
}

.button {
  display: inline-flex;
  justify-content: center;
  align-items: center;
  gap: 10px;
  border: 1px solid transparent;
  border-radius: 9px;
  padding: 12px 17px;
  font-size: 12px;
  font-weight: 500;
}

.primary {
  background: var(--accent);
  color: var(--yc-surface);
}

.primary:not(:disabled):hover {
  background: var(--yc-primary);
  box-shadow: 0 5px 12px #243b5518;
}

.secondary {
  border: 1px solid var(--yc-border);
  background: var(--yc-page);
  color: var(--yc-text);
}

.secondary:not(:disabled):hover {
  background: var(--yc-soft);
}

.auth-submit {
  width: 100%;
  margin-top: 6px;
  padding: 14px 17px;
  justify-content: space-between;
  font-size: 12px;
}

.auth-footnote {
  display: flex;
  gap: 7px;
  font-size: 9px;
  line-height: 1.9;
  color: var(--yc-muted);
  margin: 20px 0 28px;
}

.auth-footnote svg {
  flex-shrink: 0;
  margin-top: 2px;
}

.auth-card-bottom {
  border-top: 1px dashed var(--yc-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 0;
  color: var(--yc-muted);
  font-size: 10px;
}

.auth-card-bottom>span:last-child {
  font-size: 22px;
  color: var(--yc-muted);
}

.welcome-footer {
  grid-column: 1/-1;
  display: flex;
  justify-content: space-between;
  border-top: 1px solid var(--line);
  padding-top: 22px;
  margin-top: 40px;
  font-size: 10px;
  color: var(--yc-muted);
}

.workbench {
  display: grid;
  grid-template-columns: 224px minmax(0,1fr);
  max-width: 1920px;
  margin: auto;
  min-height: calc(100vh - 82px);
}

.sidebar {
  background: var(--yc-surface);
  border-right: 1px solid var(--line);
  padding: 30px 17px 18px;
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: calc(100vh - 2px);
  max-height: 1200px;
  min-height: 650px;
}

.new-trip {
  padding: 12px 13px;
  justify-content: flex-start;
  width: 100%;
  font-size: 11px;
  margin-bottom: 31px;
}

.new-trip>span {
  margin-left: auto;
  color: var(--yc-muted);
}

.sidebar-label {
  font-size: 10px;
  color: var(--yc-muted);
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 11px;
  margin-bottom: 13px;
}

.sidebar-label>span {
  font-size: 7px;
  letter-spacing: 1.1px;
}

.side-nav {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.side-nav>button {
  display: flex;
  align-items: center;
  gap: 11px;
  width: 100%;
  border: 0;
  text-align: left;
  background: transparent;
  padding: 11px 13px;
  font-size: 12px;
  border-radius: 8px;
  color: var(--yc-muted);
}

.side-nav>button>span {
  margin-left: auto;
  background: var(--yc-soft);
  border-radius: 5px;
  min-width: 20px;
  text-align: center;
  padding: 2px 5px;
  font-size: 9px;
}

.side-nav>button.selected,.side-nav>button:hover {
  background: var(--yc-soft);
  color: var(--yc-primary);
}

.trip-list-label {
  margin-top: 35px;
}

.trip-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  max-height: 330px;
  overflow: auto;
}

.trip-list>button {
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid transparent;
  background: transparent;
  border-radius: 9px;
  padding: 11px 8px;
  width: 100%;
  text-align: left;
  color: var(--yc-text);
}

.trip-list>button.selected {
  border-color: var(--yc-border);
  background: var(--yc-page);
}

.trip-list-icon {
  width: 28px;
  height: 31px;
  display: grid;
  place-items: center;
  border-radius: 6px;
  background: var(--yc-soft);
  flex-shrink: 0;
}

.trip-list strong {
  font-weight: 500;
  font-size: 11px;
  display: block;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.trip-list small {
  font-size: 9px;
  color: var(--yc-muted);
  display: block;
  margin-top: 3px;
}

.live-dot {
  display: inline-block;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--yc-text);
  flex-shrink: 0;
}

.trip-list .live-dot {
  margin-left: auto;
}

.sidebar-bottom {
  margin-top: auto;
  padding-top: 35px;
}

.slow-note {
  position: relative;
  padding: 18px 13px;
  border: 1px solid var(--yc-border);
  border-radius: 10px;
  margin-bottom: 22px;
  overflow: hidden;
  background: var(--yc-page);
}

.slow-note>span {
  font-size: 6px;
  letter-spacing: .9px;
  color: var(--yc-muted);
}

.slow-note p {
  font-size: 12px;
  line-height: 1.9;
  margin-top: 9px;
  color: var(--yc-text);
}

.slow-note>svg {
  position: absolute;
  right: 9px;
  bottom: 5px;
  transform: rotate(-20deg);
  color: var(--yc-muted);
}

.side-utility {
  display: flex;
  align-items: center;
  gap: 9px;
  border: 0;
  background: none;
  width: 100%;
  padding: 9px 12px;
  color: var(--yc-muted);
  font-size: 10px;
}

.side-utility:hover,.side-utility.selected {
  color: var(--accent);
}

.workspace {
  padding: 35px clamp(20px,3vw,48px) 20px;
  min-width: 0;
}

.workspace-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 29px;
  gap: 15px;
}

.workspace-heading h1,.utility-page h1 {
  font-size: 29px;
  font-weight: 500;
  letter-spacing: -.9px;
  margin: 10px 0 9px;
}

.workspace-heading>div>p:last-child {
  font-size: 11px;
  color: var(--yc-muted);
}

.step-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 10px;
  color: var(--yc-muted);
  white-space: nowrap;
}

.step-label>span {
  display: flex;
  align-items: center;
  gap: 2px;
  background: var(--yc-soft);
  width: 23px;
  height: 23px;
  justify-content: center;
  font-size: 8px;
  border-radius: 50%;
  color: var(--yc-muted);
}

.step-label>span.complete {
  background: var(--yc-border);
  color: var(--yc-text);
}

.step-label>i {
  width: 20px;
  height: 1px;
  background: var(--yc-border);
  margin: 0 2px;
}

.planning-layout {
  display: grid;
  grid-template-columns: minmax(300px,365px) minmax(0,1fr);
  gap: 24px;
  align-items: start;
}

.planning-panel {
  background: var(--yc-surface);
  border: 1px solid var(--line);
  border-radius: 15px;
  padding: 23px;
  min-width: 0;
}

.panel-heading {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 22px;
}

.panel-symbol {
  display: grid;
  place-items: center;
  border-radius: 9px;
  width: 35px;
  height: 35px;
  background: var(--yc-soft);
  color: var(--yc-text);
  flex-shrink: 0;
}

.panel-heading h2 {
  font-size: 15px;
  font-weight: 600;
}

.panel-heading p {
  font-size: 9px;
  color: var(--yc-muted);
  margin-top: 5px;
}

.composer {
  border: 1px solid var(--yc-border);
  background: var(--yc-page);
  border-radius: 11px;
  overflow: hidden;
}

.composer:focus-within {
  border-color: var(--yc-muted);
  box-shadow: 0 0 0 3px var(--yc-soft);
}

.composer textarea {
  border: 0;
  outline: 0;
  background: transparent;
  width: 100%;
  padding: 13px 13px 0;
  font-size: 11px;
  color: var(--yc-text);
  line-height: 1.9;
  resize: vertical;
  min-height: 75px;
}

.composer textarea::placeholder {
  color: var(--yc-muted);
}

.composer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 9px 9px 12px;
}

.composer-footer>span {
  font-size: 8px;
  color: var(--yc-muted);
}

.send-button {
  display: grid;
  place-items: center;
  width: 29px;
  height: 29px;
  border: 0;
  border-radius: 8px;
  background: var(--accent);
  color: white;
}

.or-divider {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 9px;
  color: var(--yc-muted);
  margin: 22px 0;
}

.or-divider:before,.or-divider:after {
  content: '';
  height: 1px;
  background: var(--yc-soft);
  flex: 1;
}

summary {
  cursor: pointer;
  list-style: none;
}

summary::-webkit-details-marker {
  display: none;
}

.travel-form>summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 18px;
}

.travel-form[open]>summary>svg {
  transform: rotate(180deg);
}

fieldset {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}

.fields {
  display: grid;
  grid-template-columns: minmax(0,1fr) minmax(0,1fr);
  gap: 15px 12px;
}

.fields .field {
  font-size: 10px;
}

.fields input,.fields select {
  height: 39px;
  font-size: 11px;
  padding: 8px 10px;
}

.fields input::placeholder {
  font-size: 10px;
}

.input-icon,.number-field {
  position: relative;
}

.input-icon>svg {
  position: absolute;
  left: 10px;
  top: 12px;
  color: var(--yc-muted);
}

.input-icon>input {
  padding-left: 31px;
}

.number-field>span {
  position: absolute;
  right: 11px;
  top: 11px;
  font-size: 10px;
  color: var(--yc-muted);
  pointer-events: none;
}

.number-field>input {
  padding-right: 29px;
}

input[type=number] {
  appearance: textfield;
  -moz-appearance: textfield;
}

input::-webkit-inner-spin-button {
  opacity: .25;
}

.optional {
  font-size: 8px;
  color: var(--yc-muted);
  font-weight: 400;
}

.interest-field {
  margin-top: 20px;
}

.interest-field>span {
  font-size: 10px;
  color: var(--yc-text);
}

.interest-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 11px;
}

.interest-chips button {
  display: flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--yc-border);
  border-radius: 6px;
  background: var(--yc-surface);
  color: var(--yc-muted);
  padding: 6px 9px;
  font-size: 9px;
}

.interest-chips .selected {
  border-color: var(--yc-muted);
  background: var(--yc-soft);
  color: var(--yc-text);
}

.interest-chips button.selected:first-child {
  background: #f9ecdf;
  border-color: #dbc3a3;
  color: #a8784f;
}

.custom-interest {
  font-size: 10px;
  padding: 8px 10px;
  margin-top: 9px;
  border-color: var(--yc-soft);
}

.extra-settings {
  border-top: 1px dashed var(--yc-border);
  margin-top: 18px;
  padding-top: 15px;
}

.extra-settings>summary {
  display: flex;
  gap: 5px;
  align-items: center;
  font-size: 9px;
  color: var(--yc-muted);
}

.extra-settings .fields {
  margin-top: 16px;
}

.extra-settings .field {
  margin-top: 12px;
}

.save-requirements {
  width: 100%;
  margin-top: 18px;
  font-size: 10px;
  padding: 10px;
}

.ready-summary {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 9px;
  color: var(--yc-text);
  padding: 13px 0;
  border-bottom: 1px solid var(--yc-soft);
  flex-wrap: wrap;
}

.saved-label {
  margin-left: auto;
  color: var(--yc-muted);
  font-size: 8px;
}

.generate-button {
  width: 100%;
  justify-content: space-between;
  font-size: 11px;
  padding: 13px 12px;
  margin-top: 17px;
}

.planning-hint {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 5px;
  font-size: 8px;
  color: var(--yc-muted);
  margin-top: 13px;
  line-height: 1.8;
}

.text-button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 0;
  background: none;
  color: var(--yc-text);
  font-size: 10px;
  padding: 5px 0;
}

.text-button:hover {
  color: var(--yc-primary);
  text-decoration: underline;
}

.discard-button {
  display: flex;
  margin: 9px auto 0;
  font-size: 9px;
}

.itinerary-canvas {
  border: 1px solid var(--line);
  background: var(--yc-surface);
  border-radius: 15px;
  min-width: 0;
  overflow: hidden;
}

.canvas-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid var(--line);
  padding: 17px 22px;
  color: var(--yc-muted);
  font-size: 9px;
}

.canvas-heading>span:first-child {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--yc-text);
}

.canvas-heading>span:last-child {
  font-size: 7px;
  letter-spacing: 1.2px;
}

.empty-canvas {
  padding: 37px 30px 20px;
}

.empty-canvas>h2 {
  font-family: 'Noto Serif SC','STZhongsong','Songti SC','SimSun',serif;
  font-size: 30px;
  line-height: 1.55;
  font-weight: 500;
  letter-spacing: .3px;
  margin: 13px 0;
  color: var(--yc-text);
}

.empty-canvas>p {
  font-size: 10px;
  line-height: 2;
  color: var(--yc-muted);
}

.empty-canvas>.journey-art {
  margin: 24px -5px 20px;
}

.inspiration-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 26px;
}

.inspiration-cards>button {
  display: flex;
  align-items: center;
  gap: 11px;
  border: 1px solid var(--yc-border);
  border-radius: 10px;
  padding: 11px 13px;
  background: var(--yc-surface);
  text-align: left;
}

.inspiration-cards>button:hover {
  border-color: var(--yc-muted);
  background: var(--yc-page);
}

.inspiration-cards strong {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: var(--yc-text);
}

.inspiration-cards small {
  display: block;
  font-size: 9px;
  color: var(--yc-muted);
  margin-top: 4px;
  font-weight: 400;
}

.inspiration-cards>button>svg {
  margin-left: auto;
  color: var(--yc-muted);
}

.destination-icon {
  display: grid;
  place-items: center;
  width: 35px;
  height: 37px;
  background: var(--yc-soft);
  color: var(--yc-muted);
  border-radius: 7px;
}

.destination-icon.food {
  background: #f5ecdd;
  color: #b9a079;
}

.destination-icon.culture {
  background: var(--yc-soft);
  color: var(--yc-muted);
}

.canvas-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 24px;
  padding-top: 17px;
  border-top: 1px dashed var(--yc-border);
  color: var(--yc-muted);
  font-size: 8px;
}

.canvas-bottom>span {
  display: flex;
  align-items: center;
  gap: 5px;
}

.workspace-footer {
  display: flex;
  justify-content: space-between;
  font-size: 8px;
  color: var(--yc-muted);
  margin-top: 29px;
  padding: 0 2px;
}

.workspace-footer>span:first-child {
  letter-spacing: 1px;
}

.chat-history {
  max-height: 320px;
  overflow: auto;
  scrollbar-width: thin;
  margin: 0 -5px 18px;
  padding: 0 5px;
}

.chat-history article {
  font-size: 10px;
  line-height: 1.9;
}

.user-message {
  background: var(--yc-soft);
  padding: 10px 12px;
  border-radius: 10px 10px 0 10px;
  white-space: pre-wrap;
  margin: 10px 0 12px 28px;
  color: var(--yc-text);
}

.assistant-message {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 3px 0 8px;
  color: var(--yc-muted);
}

.assistant-message>svg {
  flex-shrink: 0;
  margin-top: 3px;
  color: var(--yc-muted);
}

.assistant-message p {
  white-space: pre-wrap;
}

.notice {
  display: flex;
  gap: 9px;
  align-items: flex-start;
  background: var(--yc-soft);
  border: 1px solid var(--yc-border);
  color: var(--yc-text);
  font-size: 10px;
  line-height: 1.9;
  padding: 12px 13px;
  border-radius: 9px;
  margin-top: 15px;
  overflow-wrap: anywhere;
}

.notice>svg {
  flex-shrink: 0;
  margin-top: 3px;
}

.notice strong {
  font-size: 11px;
  font-weight: 500;
}

.notice p {
  margin-top: 4px;
}

.notice .text-button {
  display: flex;
  color: #82704a;
}

.error-banner {
  padding: 12px 14px;
  border: 1px solid #eddbcd;
  border-radius: 9px;
  background: #fff4eb;
  font-size: 11px;
  color: #aa654d;
  line-height: 1.9;
  margin-bottom: 16px;
  overflow-wrap: anywhere;
}

blockquote {
  border-left: 2px solid var(--yc-muted);
  background: var(--yc-page);
  padding: 10px 12px;
  margin: 14px 0;
  color: var(--yc-muted);
  font-size: 10px;
  line-height: 1.9;
  white-space: pre-wrap;
}

blockquote small {
  color: var(--yc-muted);
}

.has-plan {
  padding: 0 20px 20px;
}

.has-plan>.canvas-heading {
  margin: 0 -20px 20px;
}

.hotel-reason {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 11px;
  line-height: 1.8;
  color: var(--yc-muted);
  margin: 10px 0 20px;
}

.hotel-reason svg {
  flex-shrink: 0;
  margin-top: 2px;
}

.day-navigation {
  display: flex;
  gap: 7px;
  flex-wrap: wrap;
  margin: 20px 0 16px;
}

.day-navigation>button {
  padding: 8px 12px;
  border: 1px solid var(--yc-border);
  border-radius: 7px;
  background: var(--yc-surface);
  font-size: 10px;
  color: var(--yc-muted);
}

.day-navigation>button.selected {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}

.version-history {
  border-top: 1px solid var(--line);
  margin-top: 25px;
  padding-top: 19px;
}

.version-history>summary {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 11px;
  color: var(--yc-muted);
}

.version-history>summary>svg:last-child {
  margin-left: auto;
}

.version-history article {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 0;
  font-size: 11px;
  border-bottom: 1px solid var(--yc-soft);
  color: var(--yc-muted);
}

.version-history small {
  display: block;
  font-size: 9px;
  color: var(--yc-muted);
  margin-top: 4px;
}

.utility-page {
  max-width: 820px;
  margin: 10px auto 80px;
}

.utility-page h1 {
  font-size: 33px;
  margin-top: 18px;
}

.utility-intro {
  font-size: 12px;
  line-height: 1.9;
  color: var(--yc-muted);
  margin-top: 15px;
  max-width: 630px;
}

.utility-card {
  padding: 28px;
  border: 1px solid var(--line);
  border-radius: 15px;
  background: var(--yc-surface);
  margin-top: 30px;
}

.utility-card h2 {
  display: flex;
  gap: 9px;
  align-items: center;
  font-size: 17px;
  font-weight: 500;
  margin-bottom: 23px;
}

.utility-card>.field {
  margin: 20px 0;
}

.utility-card>.button {
  margin-top: 20px;
}

.utility-empty {
  padding: 24px;
  border-radius: 10px;
  background: var(--yc-page);
  font-size: 12px;
  color: var(--yc-muted);
  line-height: 1.8;
}

.saved-list {
  list-style: none;
  margin: 20px 0;
  padding: 0;
}

.saved-list>li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 15px;
  padding: 17px 0;
  border-bottom: 1px solid var(--yc-soft);
}

.saved-list strong {
  font-size: 12px;
  font-weight: 500;
}

.saved-list small {
  display: block;
  font-size: 11px;
  color: var(--yc-muted);
  margin-top: 7px;
  overflow-wrap: anywhere;
}

.quality-grid {
  display: grid;
  grid-template-columns: repeat(2,minmax(0,1fr));
  gap: 20px;
  margin-top: 25px;
}

.quality-grid>div {
  background: var(--yc-page);
  border-radius: 10px;
  padding: 20px;
}

.quality-grid>div>span {
  display: block;
  font-size: 11px;
  color: var(--yc-muted);
}

.quality-grid strong {
  display: block;
  font-size: 32px;
  font-weight: 400;
  margin-top: 12px;
  color: var(--yc-text);
}

.quality-grid strong>small {
  font-size: 15px;
}

.quality-grid>.notice {
  grid-column: 1/-1;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0,0,0,0);
  white-space: nowrap;
}

.spin {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* Shared itinerary components inherit the same quiet, paper-like palette. */

.has-plan :deep(.card),.has-plan :deep(.day-card) {
  border-color: var(--yc-border);
  border-radius: 12px;
  box-shadow: none;
  background: var(--yc-surface);
}

.has-plan :deep(.hero-card) {
  background: var(--yc-soft);
  border: 0;
  padding: 24px 20px;
}

.has-plan :deep(.city) {
  font-size: 29px;
  color: var(--yc-ink);
  font-weight: 500;
}

.has-plan :deep(.card-title),.has-plan :deep(.block-title) {
  color: var(--yc-text);
}

.has-plan :deep(.muted),.has-plan :deep(.date-range) {
  color: var(--yc-muted);
}

.has-plan :deep(.budget-item),.has-plan :deep(.weather-item),.has-plan :deep(.meal) {
  background: var(--yc-page);
  border-color: var(--yc-soft);
}

.has-plan :deep(.budget-item.total) {
  background: var(--yc-soft);
}

.has-plan :deep(.budget-value),.has-plan :deep(.meal-name) {
  color: var(--yc-text);
}

.has-plan :deep(.date-index) {
  background: var(--yc-soft);
  color: var(--yc-text);
}

.has-plan :deep(.date-day) {
  color: var(--yc-text);
}

.has-plan :deep(.day-card) {
  padding: 20px 16px;
}

.has-plan :deep(.meal) {
  border: 1px solid var(--yc-soft);
  border-left: 3px solid #c8a36c;
  padding: 12px;
  font-size: 12px;
}

.has-plan :deep(.meal-type) {
  background: #f1e7d5;
  color: #a4875c;
}

.has-plan :deep(.schedule-list) {
  background: var(--yc-page);
  border-radius: 10px;
  padding: 12px 12px 12px 30px;
}

.has-plan :deep(.schedule-list li) {
  padding: 6px 0;
}

.has-plan :deep(a) {
  color: var(--yc-text);
}

.has-plan :deep(.module-editor) {
  background: var(--yc-page);
  border: 1px solid var(--yc-border);
  border-radius: 12px;
  padding: 20px;
}

.has-plan :deep(.module-editor h3) {
  color: var(--yc-text);
}

.has-plan :deep(.module-editor button) {
  background: var(--yc-soft);
  color: var(--yc-text);
  border-color: var(--yc-border);
}

.has-plan :deep(.candidate) {
  background: var(--yc-surface);
  border-color: var(--yc-border);
}

.has-plan :deep(.candidate input) {
  accent-color: var(--yc-text);
}

.has-plan :deep(.route-list) {
  font-size: 11px;
  line-height: 1.9;
}

.has-plan :deep(.alert-card) {
  background: var(--yc-page);
  border-color: var(--yc-border);
}

.has-plan :deep(.alert-list) {
  font-size: 11px;
  line-height: 1.9;
}

.has-plan :deep(.spot-name) {
  color: var(--yc-text);
}

.has-plan :deep(.spot-category) {
  background: var(--yc-soft);
  color: var(--yc-muted);
}

.has-plan :deep(.block) {
  margin-top: 23px;
}

.has-plan :deep(.photos) {
  border-radius: 10px;
  overflow: hidden;
}

@media (min-width:1550px) {
  .workspace {
    padding-right: 55px;
    padding-left: 45px;
  }
  .planning-layout {
    grid-template-columns: 385px minmax(0,1fr);
    gap: 30px;
  }
  .empty-canvas {
    padding-left: 48px;
    padding-right: 48px;
  }
  .inspiration-cards {
    flex-direction: row;
  }
  .inspiration-cards>button {
    flex: 1;
    padding: 13px 11px;
  }
  .inspiration-cards>button>svg {
    display: none;
  }
  .empty-canvas>.journey-art {
    max-width: 720px;
    margin-left: auto;
    margin-right: auto;
  }
}

@media (max-width:1200px) {
  .topbar {
    gap: 30px;
    padding: 0 25px;
  }
  .product-note {
    display: none;
  }
  .workbench {
    grid-template-columns: 190px minmax(0,1fr);
  }
  .sidebar {
    padding-left: 12px;
    padding-right: 12px;
  }
  .workspace {
    padding: 30px 20px 20px;
  }
  .planning-layout {
    grid-template-columns: minmax(280px,330px) minmax(0,1fr);
    gap: 17px;
  }
  .planning-panel {
    padding: 19px 17px;
  }
  .step-label {
    display: none;
  }
  .empty-canvas {
    padding: 30px 22px 20px;
  }
  .empty-canvas>h2 {
    font-size: 26px;
  }
  .canvas-heading {
    padding-left: 18px;
    padding-right: 18px;
  }
  .canvas-heading>span:last-child {
    display: none;
  }
  .welcome {
    column-gap: 6%;
    padding-left: 40px;
    padding-right: 40px;
  }
  .auth-card {
    padding-left: 27px;
    padding-right: 27px;
  }
  .welcome-story h1 {
    font-size: 45px;
  }
  .welcome-story h1>svg {
    left: 120px;
  }
  .welcome-features {
    font-size: 10px;
  }
}

@media (max-width:1000px) {
  .workbench {
    grid-template-columns: 170px minmax(0,1fr);
  }
  .sidebar {
    padding: 24px 10px;
  }
  .new-trip {
    font-size: 10px;
    padding: 11px 9px;
    gap: 6px;
  }
  .new-trip>span {
    display: none;
  }
  .side-nav>button {
    font-size: 11px;
    padding: 10px;
  }
  .sidebar-label>span {
    font-size: 6px;
  }
  .planning-layout {
    grid-template-columns: 1fr;
  }
  .planning-panel {
    padding: 24px;
  }
  .empty-canvas {
    padding: 30px 40px 25px;
  }
  .empty-canvas>h2 {
    font-size: 30px;
  }
  .empty-canvas>.journey-art {
    max-width: 540px;
    margin-left: auto;
    margin-right: auto;
  }
  .welcome {
    grid-template-columns: 1fr minmax(310px,.9fr);
    padding: 45px 30px 24px;
    column-gap: 5%;
  }
  .welcome-story h1 {
    font-size: 36px;
    letter-spacing: 0;
  }
  .welcome-story h1>svg {
    left: 95px;
    width: 150px;
  }
  .welcome-copy {
    font-size: 11px;
  }
  .welcome-features {
    flex-wrap: wrap;
    gap: 14px;
    font-size: 9px;
  }
  .auth-card h2 {
    font-size: 21px;
  }
  .top-nav {
    gap: 22px;
    font-size: 12px;
  }
  .topbar-end {
    gap: 0;
  }
}

@media (max-width:720px) {
  .topbar {
    height: 68px;
    gap: 20px;
    padding: 0 18px;
  }

  .brand-icon {
    width: 34px;
    height: 34px;
    border-radius: 11px;
  }
  .top-nav {
    margin-left: auto;
    font-size: 11px;
    gap: 18px;
  }
  .top-nav a:last-child {
    display: none;
  }
  .topbar-end {
    display: none;
  }
  .welcome {
    display: flex;
    flex-direction: column;
    padding: 35px 23px 25px;
    gap: 0;
  }
  .welcome-story {
    width: 100%;
  }
  .welcome-story h1 {
    font-size: 40px;
    margin-top: 21px;
  }
  .welcome-story h1>svg {
    left: 110px;
    width: 170px;
  }
  .welcome-copy {
    font-size: 12px;
  }
  .welcome-story>.journey-art {
    max-width: 540px;
    margin: 0 auto;
  }
  .welcome-story>.eyebrow {
    font-size: 7px;
    letter-spacing: 1.7px;
  }
  .welcome-features {
    font-size: 10px;
    margin-top: 18px;
    justify-content: space-between;
    gap: 8px;
  }
  .welcome-features svg {
    width: 14px;
  }
  .auth-card {
    width: 100%;
    max-width: 460px;
    box-sizing: border-box;
    margin-top: 40px;
    margin-bottom: 0;
    padding: 27px 25px 0;
  }
  .small-compass {
    margin-bottom: 21px;
  }
  .welcome-footer {
    width: 100%;
    box-sizing: border-box;
    font-size: 8px;
    margin-top: 35px;
    padding-top: 20px;
  }
  .workbench {
    display: block;
    min-height: calc(100vh - 68px);
  }
  .sidebar {
    position: static;
    height: auto;
    min-height: 0;
    max-height: none;
    display: block;
    border-right: 0;
    border-bottom: 1px solid var(--line);
    padding: 13px 18px;
  }
  .new-trip {
    width: auto;
    padding: 9px 11px;
    display: inline-flex;
    margin: 0 16px 0 0;
    vertical-align: middle;
    font-size: 10px;
  }
  .sidebar>.sidebar-label,.trip-list,.sidebar-bottom {
    display: none;
  }
  .side-nav {
    display: inline-flex;
    flex-direction: row;
    gap: 4px;
    vertical-align: middle;
  }
  .side-nav>button {
    font-size: 0;
    gap: 0;
    padding: 9px;
  }
  .side-nav>button>span {
    font-size: 8px;
    margin-left: 4px;
  }
  .workspace {
    padding: 25px 16px 18px;
  }
  .workspace-heading {
    margin-bottom: 22px;
  }
  .workspace-heading h1 {
    font-size: 25px;
  }
  .workspace-heading .eyebrow {
    font-size: 8px;
  }
  .workspace-heading>div>p:last-child {
    font-size: 10px;
  }
  .planning-layout {
    gap: 20px;
  }
  .planning-panel {
    padding: 22px 19px;
  }
  .panel-heading h2 {
    font-size: 16px;
  }
  .panel-heading p {
    font-size: 10px;
  }
  .fields .field {
    font-size: 11px;
  }
  .fields input,.fields select {
    font-size: 12px;
    height: 42px;
  }
  .field input[type=date] {
    padding-left: 8px;
    padding-right: 4px;
    max-width: 100%;
  }
  .interest-chips button {
    font-size: 10px;
    padding: 7px 9px;
  }
  .composer textarea {
    font-size: 12px;
  }
  .empty-canvas {
    padding: 29px 22px 21px;
  }
  .empty-canvas>h2 {
    font-size: 29px;
  }
  .canvas-bottom {
    font-size: 8px;
  }
  .has-plan {
    padding-left: 13px;
    padding-right: 13px;
  }
  .has-plan>.canvas-heading {
    margin-left: -13px;
    margin-right: -13px;
  }
  .has-plan :deep(.day-card) {
    padding: 17px 13px;
  }
  .utility-page {
    margin-top: 4px;
  }
  .utility-page h1 {
    font-size: 28px;
  }
  .utility-card {
    padding: 21px;
  }
  .utility-intro {
    font-size: 11px;
  }
  .quality-grid {
    gap: 10px;
  }
  .quality-grid>div {
    padding: 15px;
  }
  .quality-grid strong {
    font-size: 27px;
  }
}

@media (prefers-reduced-motion:reduce) {
  *,*:before,*:after {
    animation: none !important;
    transition: none !important;
    scroll-behavior: auto !important;
  }
}

.mobile-trip-controls {
  display: none;
}

.auth-description {
  font-size: 12px;
}

.auth-card .field {
  font-size: 12px;
}

.auth-card .field input {
  font-size: 13px;
}

.auth-card .field input::placeholder {
  font-size: 12px;
}

.auth-footnote {
  font-size: 10px;
}

.workspace-heading>div>p:last-child {
  font-size: 12px;
}

.panel-heading p {
  font-size: 11px;
  line-height: 1.7;
}

.composer textarea {
  font-size: 12px;
}

.composer-footer>span {
  font-size: 9px;
}

.travel-form>summary {
  font-size: 13px;
}

.fields .field {
  font-size: 12px;
}

.fields input,.fields select {
  font-size: 12px;
}

.interest-field>span {
  font-size: 12px;
}

.interest-chips button {
  font-size: 11px;
}

.custom-interest,.extra-settings>summary {
  font-size: 11px;
}

.save-requirements,.generate-button {
  font-size: 12px;
}

.planning-hint {
  font-size: 10px;
}

.ready-summary {
  font-size: 11px;
}

.saved-label {
  font-size: 10px;
}

.empty-canvas>p {
  font-size: 12px;
}

.inspiration-cards strong {
  font-size: 13px;
}

.inspiration-cards small {
  font-size: 11px;
}

.canvas-bottom {
  font-size: 10px;
}

.trip-list strong {
  font-size: 12px;
}

.trip-list small {
  font-size: 10px;
}

.chat-history article {
  font-size: 12px;
}

.notice {
  font-size: 12px;
}

.text-button {
  font-size: 11px;
}

.error-banner {
  font-size: 12px;
}

.welcome-copy,.welcome-features,.auth-description,.workspace-heading>div>p:last-child,.panel-heading p,.empty-canvas>p,.inspiration-cards small,.planning-hint,.canvas-bottom {
  color: var(--yc-muted);
}

.auth-footnote,.trip-list small,.sidebar-label,.saved-label {
  color: var(--yc-muted);
}

.side-nav>button {
  color: var(--yc-muted);
}

.interest-chips button {
  color: var(--yc-muted);
}

.interest-chips .selected {
  color: var(--yc-text);
}

.field input::placeholder,.field textarea::placeholder {
  color: var(--yc-muted);
}

.workspace-footer {
  color: var(--yc-muted);
}

.has-plan :deep(.spot) {
  background: var(--yc-page);
  border: 1px solid var(--yc-soft);
}

.has-plan :deep(.spot-index) {
  background: var(--yc-primary);
}

.has-plan :deep(.spot-desc),.has-plan :deep(.hint) {
  color: var(--yc-muted);
}

.has-plan :deep(.schedule-list time) {
  color: var(--yc-text);
}

.has-plan :deep(.action-btn.primary) {
  background: var(--yc-primary);
  border-color: var(--yc-primary);
}

.has-plan :deep(.action-btn) {
  font-size: 12px;
  background: var(--yc-page);
  color: var(--yc-text);
  border-color: var(--yc-border);
}

.has-plan :deep(.action-btn.primary) {
  color: #fff;
}

.export-options :deep(.export-panel) {
  margin-top: 15px;
}

.has-plan :deep(.comfort-warning) {
  font-size: 11px;
}

@media (max-width:720px) {
  .mobile-trip-controls {
    display: flex;
    align-items: center;
    gap: 18px;
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--yc-soft);
  }
  .mobile-trip-controls>select {
    min-width: 0;
    flex: 1;
    border: 1px solid var(--yc-border);
    border-radius: 7px;
    background: var(--yc-page);
    padding: 8px;
    font-size: 11px;
    color: var(--yc-text);
  }
  .top-nav {
    gap: 12px;
    font-size: 10px;
  }
  .top-nav a:last-child {
    display: flex;
  }
  .top-nav a:last-child>svg {
    display: none;
  }
  .topbar {
    gap: 16px;
  }
  .workspace-heading>div>p:last-child {
    font-size: 11px;
  }
  .fields .field {
    font-size: 11px;
  }
  .interest-field>span {
    font-size: 11px;
  }
  .interest-chips button {
    font-size: 10px;
  }
  .canvas-bottom {
    font-size: 9px;
  }
  .composer-footer>span {
    font-size: 8px;
  }
  .planning-hint {
    font-size: 9px;
  }
}
/* Cloud workspace: shared palette, readable controls and an open inspiration rail. */
.topbar { height: 72px; background: var(--yc-surface); padding: 0 32px; gap: 55px; }
.workbench { grid-template-columns: 224px minmax(0, 1fr); min-height: calc(100vh - 72px); }
.sidebar { background: #f7faff; }
.workspace { padding: 30px 26px 20px; }
.workspace-heading { margin-bottom: 24px; }
.workspace-heading h1 { font-size: 29px; font-weight: 650; letter-spacing: 0; margin: 3px 0 10px; }
.workspace-heading>div>p:last-child { font-size: 13px; }
.planning-layout { grid-template-columns: minmax(330px, .88fr) minmax(0, 1.12fr); gap: 18px; }
.planning-panel, .itinerary-canvas { border-radius: 9px; background: var(--yc-surface); border-color: var(--yc-border); box-shadow: 0 3px 16px #243b5503; }
.panel-heading h2 { font-size: 17px; }
.panel-heading p { font-size: 11px; }
.fields .field, .interest-field>span { font-size: 12px; }
.field input, .field select, .field textarea, .custom-interest { font-size: 13px; min-height: 39px; }
.composer textarea { font-size: 13px; line-height: 1.7; }
.button { font-size: 13px; }
.button.primary:hover:not(:disabled) { background: var(--yc-primary-hover); }
.itinerary-canvas:not(.has-plan)>.canvas-heading { display: none; }
.empty-canvas { padding: 24px; }
.empty-canvas>h2 { font-family: var(--yc-font); font-size: 21px; line-height: 1.6; font-weight: 650; color: var(--yc-ink); margin: 0 0 19px; letter-spacing: 0; }
.empty-canvas>.journey-art { margin: 0; max-width: none; }
.inspiration-cards { flex-direction: column; gap: 0; margin-top: 14px; }
.inspiration-cards>button { flex: initial; padding: 18px 0; border: 0; border-bottom: 1px solid var(--yc-border); border-radius: 0; background: transparent; }
.inspiration-cards>button:last-child { border-bottom: 0; }
.inspiration-cards strong { font-size: 16px; font-weight: 600; color: var(--yc-ink); }
.inspiration-cards small { font-size: 12px; color: var(--yc-muted); margin-top: 6px; }
.inspiration-cards>button>svg { display: block; }
.destination-icon { width: 44px; height: 44px; color: var(--yc-primary); background: var(--yc-soft); }
.canvas-bottom { font-size: 11px; margin-top: 8px; }
.welcome-story h1, .auth-card h2 { font-family: var(--yc-font); }
.welcome-story h1 { font-weight: 650; }
.welcome-story h1>svg { display: none; }
.welcome-copy { font-size: 14px; }
.auth-description { font-size: 13px; }
.auth-tabs { font-size: 13px; }
.welcome-story>.journey-art { margin-top: 28px; }
@media(min-width:1550px) { .workspace { padding: 36px 34px 20px; } }
@media(max-width:1200px) { .workbench { grid-template-columns: 180px minmax(0,1fr); } .planning-layout { grid-template-columns: minmax(290px, .9fr) minmax(0, 1fr); gap: 16px; } .workspace { padding: 26px 20px; } .empty-canvas { padding: 22px; } .empty-canvas>h2 { font-size: 19px; } }
@media(max-width:1000px) { .planning-layout { grid-template-columns: 1fr; } .empty-canvas>.journey-art { max-width: none; } }
@media(max-width:720px) { .topbar { height: 66px; padding: 0 16px; gap: 12px; } .workbench { display: block; } .workspace { padding: 24px 16px; } .workspace-heading h1 { font-size: 25px; } .planning-panel { padding: 20px 16px; } .empty-canvas { padding: 20px 16px; } .empty-canvas>h2 { font-size: 19px; } .welcome-copy { font-size: 12px; } .canvas-bottom { font-size: 10px; } }
</style>

<style scoped>
.clarification-panel{background:var(--yc-soft);border:1px solid var(--yc-border);border-radius:12px;padding:16px;margin:16px 0}.clarification-panel h3{margin:0;font-size:15px}.clarification-panel p{font-size:12px;color:var(--yc-text);margin:8px 0;line-height:1.7}.clarification-panel article{padding:12px 0;border-top:1px solid var(--yc-border);font-size:12px;line-height:1.8}.question-options{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.question-options button{background:white;border:1px solid var(--yc-muted);border-radius:8px;color:var(--yc-primary);padding:9px 12px;font-size:12px;text-align:left}
</style>
