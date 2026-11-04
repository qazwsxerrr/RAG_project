<template>
  <div class="flex flex-col items-center justify-center min-h-[60vh] gap-3 text-slate-600">
    <div class="w-8 h-8 border-3 border-indigo-600 border-t-transparent rounded-full animate-spin"></div>
    <p class="text-sm font-medium">正在完成企业身份核验与安全换票...</p>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { useAuthStore } from '../stores/auth';

const router = useRouter();
const authStore = useAuthStore();

onMounted(async () => {
  try {
    await authStore.refreshToken();
    const redirectUrl = sessionStorage.getItem('rag_auth_redirect') || '/documents';
    sessionStorage.removeItem('rag_auth_redirect');
    router.replace(redirectUrl);
  } catch (err) {
    console.error('身份回调处理失败:', err);
  }
});
</script>
