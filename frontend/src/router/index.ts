import { createRouter, createWebHistory } from 'vue-router'

import HomeView from '../views/HomeView.vue'

/**
 * 路由表。
 *
 * Day 5 待实现：
 *   /            行程规划页（表单 + 结果）
 *   /trip/:id    历史行程页（依赖后端 Checkpointer，Day 7 之后）
 */
export default createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'home',
      component: HomeView,
    },
  ],
})
