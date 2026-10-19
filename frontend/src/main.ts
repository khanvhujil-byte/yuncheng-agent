import { createApp } from 'vue'
import { Button, Checkbox, ConfigProvider, DatePicker, Form, Input, InputNumber, Select } from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'

import App from './App.vue'
import router from './router'

const app = createApp(App)
for (const component of [Button, Checkbox, ConfigProvider, DatePicker, Form, Input, InputNumber, Select]) app.use(component)
app.use(router).mount('#app')
