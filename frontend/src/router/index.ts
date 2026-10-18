import { createRouter, createWebHistory } from 'vue-router'

import CommunityView from '../views/CommunityView.vue'
import HomeView from '../views/HomeView.vue'
import ChatView from '../views/ChatView.vue'

/**
 * 路由表。
 *
 *   /            行程规划页（表单 + 结果 + 发布到社区）
 *   /community   社区行程榜（Java 服务提供数据：排行榜 / 点赞 / 热度）
 *
 * 两个页面分属两个后端服务，但用户视角只需要"规划"和"看别人去哪"两件事 ——
 * 所以前端把它们放在同一个导航里，不暴露"这是两个服务"的实现细节。
 */
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/chat', name: 'chat', component: ChatView },
    {
      path: '/',
      name: 'home',
      component: HomeView,
    },
    {
      path: '/community',
      name: 'community',
      component: CommunityView,
    },
  ],
})
