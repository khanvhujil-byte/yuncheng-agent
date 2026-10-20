<template>
  <a-config-provider :theme="{ token: { colorPrimary: '#3375d6', colorText: '#243b55', colorTextSecondary: '#6b8098', colorBorder: '#dce7f4', borderRadius: 8, colorBgContainer: '#ffffff', fontFamily: 'Inter, Segoe UI, Microsoft YaHei, sans-serif' } }">
    <router-view />
  </a-config-provider>
</template>
<style>
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: var(--yc-page); color: var(--yc-ink); font-family: var(--yc-font); -webkit-font-smoothing: antialiased; }
#app { min-height: 100vh; }
</style>
