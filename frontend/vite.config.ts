import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendDir = fileURLToPath(new URL('.', import.meta.url))

// dev :9000，/api 代理到后端 :8000，build 输出到 backend/static
export default defineConfig({
  // Relative assets let one build run standalone at "/" and behind "/ifund".
  base: './',
  plugins: [react()],
  server: {
    port: 9000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: '../backend/static',
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      preserveEntrySignatures: 'strict',
      input: {
        main: resolve(frontendDir, 'index.html'),
        'ifund-embed': resolve(frontendDir, 'src/embed.tsx'),
      },
      output: {
        entryFileNames: 'assets/[name].js',
        assetFileNames: (assetInfo) => (
          assetInfo.name?.endsWith('.css')
            ? 'assets/ifund.css'
            : 'assets/[name]-[hash][extname]'
        ),
      },
    },
  },
})
