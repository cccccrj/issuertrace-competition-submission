import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

import pxToRem from './postcss-px-to-rem.js'

export default defineConfig({
  plugins: [vue()],
  // 样式源码写 px，产物是 rem，界面随根字号缩放。原因见插件文件头。
  css: { postcss: { plugins: [pxToRem()] } },
  server: {
    // The dev server proxies to the API so the client uses one origin in both
    // development and production, and never needs a base-URL switch.
    proxy: {
      // cookieDomainRewrite keeps the session cookie usable in development: the
      // API sets it for 127.0.0.1 while the dev server may be reached as
      // localhost, and a cookie scoped to the wrong host silently never returns.
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true, cookieDomainRewrite: '' },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // ECharts gets its own chunk so the shell and the sign-in page do not
        // pay for a charting library they never draw with.  Verified by
        // comparing the shell chunk's size before and after it was added.
        manualChunks: { echarts: ['echarts/core', 'echarts/charts', 'echarts/components', 'echarts/renderers'] },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/__tests__/setup.js'],
    include: ['src/**/*.test.js'],
  },
})
