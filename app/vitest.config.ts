import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    // Fast timeouts
    testTimeout: 5000,
    hookTimeout: 5000,
  },
  resolve: {
    alias: {
      'react-native': 'react-native-web',
    },
  },
})
