import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The resume page and the webhook endpoint are served by FastAPI, so they are
// proxied alongside /api. That keeps every link on one origin during the demo.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/orders': 'http://localhost:8000',
      '/webhooks': 'http://localhost:8000',
    },
  },
})
