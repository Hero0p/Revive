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
      '/api': 'https://revive-revenue.onrender.com/',
      '/orders': 'https://revive-revenue.onrender.com/',
      '/webhooks': 'https://revive-revenue.onrender.com/',
    },
  },
})
