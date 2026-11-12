import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import path from 'path';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src')
    }
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return;
          if (id.includes('markdown-it')) {
            return 'markdown-vendor';
          }
          if (/node_modules[\\/](vue|vue-router|pinia|@vue)[\\/]/.test(id)) {
            return 'vue-vendor';
          }
        }
      }
    }
  },
  server: {
    port: 5173,
    host: '0.0.0.0',
    watch: {
      usePolling: true,
      interval: 500,
    },
    proxy: {
      '/api/admin': {
        target: 'http://localhost:8080',
        changeOrigin: true
      },
      // OIDC 授权与回调必须保持同源：changeOrigin=false 保留 Host=localhost:5173，
      // 否则 Java 会把 {baseUrl} 推导为 localhost:8080，导致 redirect_uri 与 IdP 注册值不符，
      // 且回调后 /auth/callback 落到 Java 端口被 denyAll() 拦截。
      '/oauth2': {
        target: 'http://localhost:8080',
        changeOrigin: false
      },
      '/login/oauth2': {
        target: 'http://localhost:8080',
        changeOrigin: false
      },
      '/api/v1': {
        target: 'http://localhost:8001',
        changeOrigin: true
      }
    }
  }
});
