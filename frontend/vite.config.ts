import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      /**
       * 社区服务（Java，8080）走**同源反代**。
       *
       * 为什么前缀是 /community-api 而不是 /community（这一条是被真 bug 逼出来的）：
       *   前端有一个页面路由就是 /community。如果代理前缀也叫 /community，
       *   Vite 的代理会**把整页导航也接走**（它不只拦 XHR），于是：
       *       点浏览器刷新 /community  ->  被转发到 Java:8080/community
       *       Spring 的 context-path 返回 302 -> 浏览器跳到 8080 -> 404
       *   结果就是"从首页点链接能进，直接输网址/刷新就白屏"—— 而且所有接口调用都是通的，
       *   光看网络请求完全发现不了。真机浏览器一测就露。
       *
       * 所以定一条不会撞名的规则：**API 前缀一律带 -api 后缀**，前端路由随便用。
       * 生产环境 deploy/nginx.conf 用的是同一个前缀，两套环境行为一致。
       */
      '/community-api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
        // Java 侧真实的路径是 /community/**，这里做前缀替换：
        // /community-api/leaderboard  ->  http://127.0.0.1:8080/community/leaderboard
        rewrite: (path) => path.replace(/^\/community-api/, '/community'),
      },
    },
  },
})
