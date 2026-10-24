import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development the app talks to the FastAPI backend (backend/main.py, port 8000).
// After `npm run build`, FastAPI serves the built app itself, so no proxy is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://localhost:8000' },
  },
})
