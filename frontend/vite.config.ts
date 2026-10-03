import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { buildDefine } from './build-meta.ts'

export default defineConfig({
  plugins: [react()],
  define: buildDefine,
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
