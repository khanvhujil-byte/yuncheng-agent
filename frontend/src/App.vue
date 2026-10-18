<template>
  <a-config-provider :theme="{ token: { colorPrimary: '#315a43', colorText: '#304331', colorBorder: '#dde3d3', borderRadius: 9, colorBgContainer: '#fffefa', fontFamily: 'Inter, Segoe UI, Microsoft YaHei, sans-serif' } }">
    <router-view />
  </a-config-provider>
</template>

<style>
* {
  box-sizing: border-box;
}

html,
body {
  margin: 0;
  padding: 0;
}

body {
  background: #f7f7f1;
  color: #304331;
  font-family: system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC',
    sans-serif;
  -webkit-font-smoothing: antialiased;
}

#app {
  min-height: 100vh;
}
</style>
