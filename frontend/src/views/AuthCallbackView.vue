<template>
  <div class="flex flex-col items-center justify-center min-h-[60vh] px-4 text-slate-600">
    <div v-if="isLoading" class="flex flex-col items-center gap-3">
      <div class="w-8 h-8 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin"></div>
      <p class="text-sm font-medium">正在登录，请稍候...</p>
    </div>

    <div
      v-else-if="authStore.authError"
      class="w-full max-w-md rounded-xl border border-rose-200 bg-white p-6 shadow-sm space-y-4 text-left"
    >
      <div class="flex items-start gap-3">
        <div class="w-9 h-9 rounded-lg bg-rose-50 text-rose-600 flex items-center justify-center shrink-0 font-bold text-sm">
          !
        </div>
        <div class="min-w-0 space-y-1">
          <div class="flex items-center gap-2 flex-wrap">
            <h2 class="text-sm font-bold text-slate-900">
              {{ authStore.authError.isLoopBlocked ? '登录重定向已熔断' : '登录验证失败' }}
            </h2>
            <span
              v-if="authStore.authError.status"
              class="px-1.5 py-0.5 rounded bg-rose-50 text-rose-700 font-mono text-[11px] font-semibold"
            >
              HTTP {{ authStore.authError.status }}
            </span>
          </div>
          <p class="text-xs text-slate-600 leading-relaxed break-words">
            {{ authStore.authError.message }}
          </p>
        </div>
      </div>

      <div class="flex items-center justify-end gap-2 pt-2 border-t border-slate-100">
        <button
          @click="handleRetry"
          class="px-3 py-1.5 rounded-lg text-xs font-medium text-slate-700 bg-slate-100 hover:bg-slate-200 transition-colors cursor-pointer"
        >
          重试验证
        </button>
        <button
          @click="handleRelogin"
          class="px-3 py-1.5 rounded-lg text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 transition-colors cursor-pointer"
        >
          重新登录 / 切换账号
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { useAuthStore } from '../stores/auth';

const router = useRouter();
const authStore = useAuthStore();
const isLoading = ref(true);

const exchangeAndRedirect = async () => {
  isLoading.value = true;
  authStore.authError = null;
  try {
    await authStore.refreshToken({ redirectOnUnauthorized: false });
    const redirectUrl = sessionStorage.getItem('rag_auth_redirect') || '/documents';
    sessionStorage.removeItem('rag_auth_redirect');
    router.replace(redirectUrl);
  } catch (err) {
    console.error('身份回调处理失败:', err);
    isLoading.value = false;
  }
};

const handleRetry = () => {
  void exchangeAndRedirect();
};

const handleRelogin = () => {
  void authStore.logout();
};

onMounted(() => {
  if (authStore.authError) {
    isLoading.value = false;
    return;
  }
  void exchangeAndRedirect();
});
</script>
