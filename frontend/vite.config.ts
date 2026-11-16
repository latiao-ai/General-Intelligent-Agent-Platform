import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'

const page = (name: string) => fileURLToPath(new URL(`${name}.html`, import.meta.url))

// 多页工程：每迁移一个页面，就在 input 里加一项。
// base 必须是 /app/，后端把 dist/assets 挂在 /app/assets 上。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/app/',
  resolve: {
    // 与 tsconfig.json 的 paths 保持一致，两处必须同时改
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: {
        plaza: page('plaza'),
        bots: page('bots'),
        messaging: page('messaging'),
        admin: page('admin'),
        hub: page('hub'),
      },
      output: {
        // React 运行时单独一个 chunk。页面越迁越多时，这份只下载一次、长期命中缓存；
        // 每个新页面只多出自己的业务代码。
        manualChunks: {
          'react-vendor': ['react', 'react-dom', 'react-dom/client'],
        },
      },
    },
  },
  server: {
    port: 5174,
    // dev 模式下直接把接口请求转给本机 FastAPI，行为与生产一致
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  // preview 也用同一套代理：排查"构建产物 + 真接口"的问题时，
  // 不必为了看一眼 dist 就重启后端
  preview: {
    port: 4173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8002', changeOrigin: true },
    },
  },
})
