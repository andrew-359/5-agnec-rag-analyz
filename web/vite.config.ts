import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: { outDir: 'dist/client' },
  server: { port: 4311, strictPort: true, proxy: {
    '/trpc': process.env.RAG_API_ORIGIN || 'http://127.0.0.1:4310',
    '/api': process.env.RAG_API_ORIGIN || 'http://127.0.0.1:4310',
    '/published': process.env.RAG_API_ORIGIN || 'http://127.0.0.1:4310',
  } },
});
