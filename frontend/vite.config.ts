import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // xfwd sends X-Forwarded-Host, so the backend's same-origin check on
      // writes accepts the dev server's Origin.
      '/api': { target: 'http://127.0.0.1:7860', changeOrigin: true, xfwd: true },
    },
  },
})

