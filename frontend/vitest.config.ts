import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { buildDefine } from './build-meta.ts'

export default defineConfig({
  plugins: [react()],
  // Same injected build constants as vite.config.ts, so components that render
  // the version (AUD-407) behave in tests exactly as they do in the bundle.
  define: buildDefine,
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      exclude: ['node_modules/', 'src/test/'],
    },
  },
})
