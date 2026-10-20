<script setup lang="ts">
/**
 * 社区行程榜 —— **前端第一次同时连两个后端服务**。
 *
 * 页面上的数据来源分两路：
 *   1. Java（/community/**，经 Vite 代理）：排行榜、点赞    ← 业务与高并发
 *   2. Python（/api/community/hot）：热度统计               ← 它再去读 Java（反哺那条边）
 *
 * 为什么点赞要做"乐观更新"：
 *   点赞是高频、低风险的写操作。等服务端返回再改数字，用户会感觉卡；
 *   所以先本地 +1 并把按钮置为已赞，失败了再回滚并提示。
 *   **回滚这一步不能省** —— 否则界面会显示一个服务端并不承认的点赞数。
 *
 * 为什么每个卡片要单独问一次"我点过没"：
 *   榜单是**公共数据**（所有人看到的一样），可以缓存；
 *   而"我点过没"是**每人不同的数据**，不能塞进公共榜单里。
 *   所以加载完榜单后并发补齐这 20 个状态 —— 这是数据性质决定的，不是偷懒。
 */
import { ArrowLeft, Flame, Heart, LoaderCircle, RefreshCw, Sparkles } from 'lucide-vue-next'
import BrandLockup from '@/components/BrandLockup.vue'
import { computed, onMounted, ref } from 'vue'

import {
  ApiError,
  fetchCommunityHot,
  fetchLeaderboard,
  fetchLikeStatus,
  getCommunityUserId,
  likeItinerary,
} from '@/services/api'
import type { HotStats, LeaderboardItem } from '@/types/trip'

type BoardType = 'hot' | 'new'

const boardType = ref<BoardType>('hot')
const items = ref<LeaderboardItem[]>([])
const hotStats = ref<HotStats>({})
const hotAvailable = ref(false)
const hotLoading = ref(true)
const hotMessage = ref('')

const loading = ref(false)
const errorMessage = ref('')
const likePending = ref<Set<number>>(new Set())
const likedIds = ref<Set<number>>(new Set())
const toast = ref('')

const userId = getCommunityUserId()

const emptyText = computed(() =>
  boardType.value === 'hot'
    ? '社区还没有行程。先去生成一份，然后在结果页点「发布到社区」。'
    : '还没有人发布过行程。',
)

const hotCities = computed(() => hotStats.value.city ?? [])
const hotPreferences = computed(() => hotStats.value.preference ?? [])

async function loadBoard() {
  loading.value = true
  errorMessage.value = ''
  try {
    const list = await fetchLeaderboard(boardType.value, 20)
    items.value = list
    await fillLikeStatus(list)
  } catch (error) {
    errorMessage.value =
      error instanceof ApiError
        ? error.message
        : '社区服务暂时不可用，请稍后重试。'
    items.value = []
  } finally {
    loading.value = false
  }
}

/** 并发补齐"我点过没"（每人不同的数据，只能单独问）。 */
async function fillLikeStatus(list: LeaderboardItem[]) {
  const results = await Promise.allSettled(
    list.map((item) => fetchLikeStatus(item.itineraryId)),
  )
  const liked = new Set<number>()
  results.forEach((result, index) => {
    if (result.status === 'fulfilled' && result.value.hasLiked) {
      liked.add(list[index].itineraryId)
    }
  })
  likedIds.value = liked
}

async function loadHot() {
  hotLoading.value = true
  try {
    const response = await fetchCommunityHot(5)
    hotStats.value = response.stats ?? {}
    hotAvailable.value = response.available
    hotMessage.value = response.message
  } catch {
    // 热度只是锦上添花，失败了不打扰用户（榜单本身的错误才需要提示）
    hotAvailable.value = false
    hotMessage.value = '热度数据暂时不可用。'
  } finally {
    // ★ 区分"正在加载"和"真的失败"：原先两者共用一句"暂时不可用"，
    //   于是页面刚打开的那一秒看起来像坏了（真机验证时被抓到的观感问题）。
    hotLoading.value = false
  }
}

async function onLike(item: LeaderboardItem) {
  const id = item.itineraryId
  if (likePending.value.has(id)) return

  // ---- 乐观更新：先改界面，再落服务端 ----
  const previousCount = item.likeCount
  const wasLiked = likedIds.value.has(id)
  likePending.value = new Set(likePending.value).add(id)
  const nextLiked = new Set(likedIds.value)
  nextLiked.add(id)
  likedIds.value = nextLiked
  item.likeCount = previousCount + (wasLiked ? 0 : 1)

  try {
    const result = await likeItinerary(id)
    // 服务端是权威：用它返回的数字覆盖本地猜测（并发场景下别人的赞也会加进来）
    item.likeCount = result.likeCount
    const settled = new Set(likedIds.value)
    if (result.errorCode) {
      // 限频 / 已下架等业务失败 —— 回滚按钮状态并说明原因
      settled.delete(id)
      toast.value = result.message
    } else if (result.liked) {
      toast.value = `点赞成功，当前 ${result.likeCount} 赞`
    } else {
      toast.value = '你已经点过赞了'
    }
    likedIds.value = settled
  } catch (error) {
    // ---- 失败回滚（不能省）----
    item.likeCount = previousCount
    const rolled = new Set(likedIds.value)
    if (!wasLiked) rolled.delete(id)
    likedIds.value = rolled
    toast.value = error instanceof ApiError ? error.message : '点赞失败，请重试。'
  } finally {
    const pending = new Set(likePending.value)
    pending.delete(id)
    likePending.value = pending
    window.setTimeout(() => (toast.value = ''), 3200)
  }
}

async function switchBoard(type: BoardType) {
  if (boardType.value === type) return
  boardType.value = type
  await loadBoard()
}

function rankClass(index: number) {
  return index < 3 ? `rank top${index + 1}` : 'rank'
}

onMounted(async () => {
  await Promise.all([loadBoard(), loadHot()])
})
</script>

<template>
  <div class="page">
    <header class="hero">
      <div class="brand">
        <router-link to="/chat" aria-label="云程，回到旅行工作台"><BrandLockup /></router-link><span class="community-title">社区行程榜</span>
      </div>
      <p class="subtitle">
        看看大家怎样出发，收藏下一次旅行的灵感。
      </p>
      <div class="ids">
        <span class="chip">账号状态：{{ userId }}</span>
        <span class="chip hint-chip">为喜欢的旅行计划点个赞</span>
      </div>
    </header>

    <div class="layout">
      <main class="main">
        <!-- 榜单类型 -->
        <div class="board-head">
          <div class="tabs">
            <button
              type="button"
              :class="['tab', boardType === 'hot' ? 'active' : '']"
              @click="switchBoard('hot')"
            >
              <Flame :size="15" /> 热门榜
            </button>
            <button
              type="button"
              :class="['tab', boardType === 'new' ? 'active' : '']"
              @click="switchBoard('new')"
            >
              <Sparkles :size="15" /> 最新榜
            </button>
          </div>
          <button class="refresh" type="button" :disabled="loading" @click="loadBoard">
            <RefreshCw :size="14" :class="loading ? 'spin' : ''" />
            刷新
          </button>
        </div>

        <p v-if="boardType === 'hot'" class="board-note">
          结合点赞与发布时间排序，发现最近值得参考的旅行计划。
        </p>
        <p v-else class="board-note">按发布时间排序，看看大家最近的新行程。</p>

        <!-- 状态：加载中 / 出错 / 空 / 列表 -->
        <div v-if="loading" class="state">
          <LoaderCircle :size="18" class="spin" />
          正在读取榜单…
        </div>

        <div v-else-if="errorMessage" class="state error">
          <span>{{ errorMessage }}</span>
          <button class="action-btn" type="button" @click="loadBoard">重试</button>
          <span class="tiny">
            稍后重试，或先去规划自己的旅行。
          </span>
        </div>

        <div v-else-if="items.length === 0" class="state">
          <span>{{ emptyText }}</span>
          <router-link class="action-btn primary" to="/">去生成行程</router-link>
        </div>

        <ul v-else class="cards">
          <li v-for="(item, index) in items" :key="item.itineraryId" class="card">
            <div :class="rankClass(index)">{{ index + 1 }}</div>
            <div class="body">
              <div class="title-row">
                <h3>{{ item.title }}</h3>
                <span class="meta">{{ item.city }} · {{ item.travelDays }} 天</span>
              </div>
              <p v-if="item.aiSummary" class="summary">{{ item.aiSummary }}</p>
              <p v-else class="summary muted">（这条行程没有简介）</p>
              <div class="foot">
                <span v-if="item.hotScore !== undefined" class="score">
                  <Flame :size="13" /> 热度 {{ item.hotScore }}
                </span>
                <span class="likes">{{ item.likeCount }} 赞</span>
              </div>
            </div>
            <button
              type="button"
              :class="['like', likedIds.has(item.itineraryId) ? 'liked' : '']"
              :disabled="likePending.has(item.itineraryId)"
              @click="onLike(item)"
            >
              <LoaderCircle v-if="likePending.has(item.itineraryId)" :size="16" class="spin" />
              <Heart v-else :size="16" :fill="likedIds.has(item.itineraryId) ? 'currentColor' : 'none'" />
              {{ likedIds.has(item.itineraryId) ? '已赞' : '点赞' }}
            </button>
          </li>
        </ul>

        <router-link class="back" to="/">
          <ArrowLeft :size="14" /> 返回行程规划
        </router-link>
      </main>

      <!-- 侧栏：热度反哺的可见证据 -->
      <aside class="side">
        <div class="card-block">
          <div class="block-title"><Flame :size="15" /> 社区热度</div>
          <p class="block-hint">
            从大家分享的行程中，看看近期常去的城市和喜欢的旅行方式。
          </p>
          <p v-if="hotLoading" class="unavailable">正在读取热度…</p>
          <template v-else-if="hotAvailable">
            <div class="group">
              <span class="group-label">热门城市</span>
              <div class="chips">
                <span v-for="city in hotCities" :key="city.key" class="chip">
                  {{ city.key }} <em>{{ city.score }}</em>
                </span>
                <span v-if="hotCities.length === 0" class="chip empty">暂无</span>
              </div>
            </div>
            <div class="group">
              <span class="group-label">常见偏好</span>
              <div class="chips">
                <span v-for="pref in hotPreferences" :key="pref.key" class="chip">
                  {{ pref.key }} <em>{{ pref.score }}</em>
                </span>
                <span v-if="hotPreferences.length === 0" class="chip empty">
                  暂无（要有人发布行程才会累积）
                </span>
              </div>
            </div>
          </template>
          <p v-else class="unavailable">{{ hotMessage || '热度数据暂时不可用。' }}</p>
        </div>

        <div class="card-block">
          <div class="block-title">把灵感变成自己的行程</div>
          <ul class="facts">
            <li>选好目的地和出发日期，写下这次的旅行想法。</li>
            <li>一起考虑景点、美食、住宿和预算。</li>
            <li>已有行程也可以按具体模块调整。</li>
          </ul>
        </div>
      </aside>
    </div>

    <transition name="fade">
      <div v-if="toast" class="toast">{{ toast }}</div>
    </transition>
  </div>
</template>

<style scoped>
.page {
  max-width: 1180px;
  margin: 0 auto;
  padding: 28px 20px 60px;
}

.hero {
  margin-bottom: 20px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 20px;
  font-weight: 600;
}

.subtitle {
  margin: 6px 0 10px;
  color: var(--yc-text);
  font-size: 13px;
}

.ids {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--yc-soft);
  border-radius: 999px;
  padding: 3px 10px;
  font-size: 12px;
  color: #3d4552;
}

.chip em {
  font-style: normal;
  color: var(--yc-muted);
}

.chip.empty {
  color: #9aa2ad;
}

.hint-chip {
  background: var(--yc-page);
  color: var(--yc-muted);
}

.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 18px;
  align-items: start;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }
}

.main,
.card-block {
  background: #fff;
  border: 1px solid var(--yc-border);
  border-radius: 12px;
}

.main {
  padding: 18px 20px 22px;
}

.board-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.tabs {
  display: flex;
  gap: 6px;
}

.tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid var(--yc-border);
  background: #fff;
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 13px;
  color: #4a5261;
  cursor: pointer;
}

.tab.active {
  background: var(--yc-ink);
  border-color: var(--yc-ink);
  color: #fff;
}

.refresh {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: none;
  background: transparent;
  color: var(--yc-muted);
  font-size: 13px;
  cursor: pointer;
}

.refresh:disabled {
  opacity: 0.6;
  cursor: default;
}

.board-note {
  margin: 10px 0 14px;
  font-size: 12px;
  color: var(--yc-muted);
  line-height: 1.6;
}

.state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 34px 10px;
  color: var(--yc-muted);
  font-size: 13px;
  text-align: center;
}

.state.error {
  color: #b4453f;
}

.tiny {
  font-size: 12px;
  color: #9aa2ad;
}

.cards {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.card {
  display: flex;
  align-items: center;
  gap: 12px;
  border: 1px solid #eef0f4;
  border-radius: 10px;
  padding: 12px 14px;
  transition: border-color 0.15s;
}

.card:hover {
  border-color: #dfe3ea;
}

.rank {
  flex: none;
  width: 26px;
  height: 26px;
  border-radius: 8px;
  background: #f1f3f7;
  color: var(--yc-muted);
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}

.rank.top1 {
  background: #fff1d6;
  color: #b07a13;
}

.rank.top2 {
  background: var(--yc-soft);
  color: var(--yc-text);
}

.rank.top3 {
  background: #fbe9df;
  color: #a8623a;
}

.body {
  flex: 1;
  min-width: 0;
}

.title-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.title-row h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.meta {
  font-size: 12px;
  color: #8b939f;
}

.summary {
  margin: 5px 0 0;
  font-size: 13px;
  color: #4a5261;
  line-height: 1.6;
}

.summary.muted {
  color: #a8aeb8;
}

.foot {
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: var(--yc-muted);
}

.score {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  color: #c2724a;
}

.like {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid #f2c9c6;
  background: #fff;
  color: #c0453f;
  border-radius: 999px;
  padding: 6px 14px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}

.like:hover:not(:disabled) {
  background: #fff5f4;
}

.like.liked {
  background: #c0453f;
  border-color: #c0453f;
  color: #fff;
}

.like:disabled {
  opacity: 0.7;
  cursor: default;
}

.back {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-top: 16px;
  font-size: 13px;
  color: var(--yc-muted);
  text-decoration: none;
}

.side {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.card-block {
  padding: 16px 18px;
}

.block-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 10px;
}

.block-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--yc-muted);
  line-height: 1.7;
}

.group {
  margin-bottom: 10px;
}

.group-label {
  display: block;
  font-size: 12px;
  color: #8b939f;
  margin-bottom: 5px;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.unavailable {
  margin: 0;
  font-size: 12px;
  color: #a8aeb8;
}

.facts {
  margin: 0;
  padding-left: 16px;
  font-size: 12px;
  color: var(--yc-text);
  line-height: 1.9;
}

.action-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid var(--yc-border);
  background: #fff;
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 13px;
  color: #3d4552;
  cursor: pointer;
  text-decoration: none;
}

.action-btn.primary {
  background: var(--yc-ink);
  border-color: var(--yc-ink);
  color: #fff;
}

.spin {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.toast {
  position: fixed;
  left: 50%;
  bottom: 32px;
  transform: translateX(-50%);
  background: var(--yc-ink);
  color: #fff;
  padding: 10px 18px;
  border-radius: 10px;
  font-size: 13px;
  box-shadow: 0 8px 24px rgba(20, 24, 32, 0.18);
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.25s;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
.hero { padding: 20px 0 24px; border-bottom: 1px solid var(--yc-border); }
.brand { flex-wrap: wrap; gap: 20px; }
.brand>a { display: inline-flex; text-decoration: none; }
.tab.active, .action-btn.primary { background: var(--yc-primary); border-color: var(--yc-primary); }
.community-title { font-size: 17px; font-weight: 500; border-left: 1px solid var(--yc-border); padding-left: 20px; }
.subtitle { margin: 18px 0 12px; }
@media(max-width:600px) { .community-title { font-size: 14px; } .brand { gap: 12px; } }
</style>
