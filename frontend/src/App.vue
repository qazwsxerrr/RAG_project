<template>
  <div class="app-layout h-screen w-screen flex overflow-hidden text-slate-800 relative selection:bg-indigo-500 selection:text-white bg-[#FAFBFD]">
    <!-- 左侧全局常驻精致导航侧边栏 (100% 对齐参考图清爽分类侧栏) -->
    <aside class="w-60 h-screen bg-white border-r border-slate-200/80 flex flex-col justify-between shrink-0 select-none z-30">
      
      <div class="flex-1 flex flex-col overflow-hidden">
        <!-- 侧边栏顶部：应用品牌 -->
        <div class="p-4 pb-3 flex items-center justify-between border-b border-slate-100">
          <div class="flex items-center space-x-2.5">
            <div class="w-7 h-7 rounded-lg bg-blue-600 flex items-center justify-center text-white">
              <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
                <polygon points="12 2 2 7 12 12 22 7 12 2"/>
                <polyline points="2 17 12 22 22 17"/>
                <polyline points="2 12 12 17 22 12"/>
              </svg>
            </div>
            <div>
              <h1 class="text-xs font-bold text-slate-900 tracking-tight">RAG 企业知识库</h1>
              <p class="text-[10px] text-slate-400">多模态检索与推理平台</p>
            </div>
          </div>

          <!-- 设置按钮 -->
          <button 
            @click="showSettingsModal = true"
            class="text-slate-400 hover:text-slate-600 p-1.5 rounded-lg hover:bg-slate-100 transition-colors"
            title="系统设置"
          >
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="3"/>
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
            </svg>
          </button>
        </div>

        <!-- 导航分组 1：核心功能 (对齐参考图侧边栏结构) -->
        <div class="px-3 pt-3 pb-1 space-y-0.5">
          <div class="px-2 py-1 text-[11px] font-semibold text-slate-400">
            核心工作台
          </div>

          <router-link 
            to="/retrieval" 
            class="flex items-center space-x-2.5 px-2.5 py-2 rounded-lg text-xs transition-colors"
            :class="route.path === '/retrieval' ? 'bg-blue-50 text-blue-600 font-semibold' : 'text-slate-600 hover:bg-slate-100/70 hover:text-slate-900'"
          >
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
            <span>智能问答</span>
          </router-link>

          <router-link 
            to="/documents" 
            class="flex items-center space-x-2.5 px-2.5 py-2 rounded-lg text-xs transition-colors"
            :class="route.path === '/documents' ? 'bg-blue-50 text-blue-600 font-semibold' : 'text-slate-600 hover:bg-slate-100/70 hover:text-slate-900'"
          >
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
            <span>文档列表</span>
          </router-link>

          <router-link 
            to="/documents/new" 
            class="flex items-center space-x-2.5 px-2.5 py-2 rounded-lg text-xs transition-colors"
            :class="route.path.startsWith('/documents/new') ? 'bg-blue-50 text-blue-600 font-semibold' : 'text-slate-600 hover:bg-slate-100/70 hover:text-slate-900'"
          >
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
            <span>提交入库</span>
          </router-link>
        </div>

        <!-- 导航分组 2：问答会话管理 (在问答页面时激活) -->
        <div v-if="route.path === '/retrieval'" class="flex-1 flex flex-col min-h-0 px-3 pt-3">
          <div class="px-2 py-1 flex items-center justify-between text-[11px] font-semibold text-slate-400">
            <span>会话记录</span>
            <button 
              @click="handleNewChat" 
              class="text-blue-600 hover:text-blue-700 text-xs font-medium cursor-pointer"
            >
              + 新建
            </button>
          </div>

          <div class="flex-1 overflow-y-auto space-y-0.5 pt-1">
            <div 
              v-for="session in chatStore.sessions" 
              :key="session.id"
              @click="handleSelectSession(session.id)"
              class="group px-2.5 py-1.5 rounded-lg cursor-pointer transition-colors text-xs flex items-center justify-between"
              :class="[
                chatStore.currentSessionId === session.id
                  ? 'bg-slate-100 text-slate-900 font-medium'
                  : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
              ]"
            >
              <span class="truncate pr-1">{{ session.title }}</span>
              <button 
                @click.stop="handleDeleteSession(session.id)"
                class="opacity-0 group-hover:opacity-100 text-slate-400 hover:text-rose-600 transition-opacity p-0.5"
                title="删除会话"
              >
                <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
              </button>
            </div>
          </div>
        </div>

        <div v-else class="flex-1"></div>
      </div>

      <!-- 侧边栏底部：真实组件监控状态栏 (Fail-Fast: 动态探针) -->
      <div class="border-t border-slate-100 p-3.5 space-y-1.5 text-[11px] text-slate-500 font-mono bg-slate-50/50">
        <div class="flex items-center space-x-2">
          <span class="w-2 h-2 rounded-full shrink-0" :class="systemStatus.dbStatus === 'online' ? 'bg-emerald-500' : 'bg-rose-500'"></span>
          <span class="truncate">PostgreSQL 16 + pgvector</span>
        </div>
        <div class="flex items-center space-x-2 text-slate-400">
          <span class="w-2 h-2 rounded-full shrink-0" :class="systemStatus.modelName ? 'bg-emerald-500' : 'bg-amber-400'"></span>
          <span class="truncate">{{ systemStatus.modelName || '大模型服务' }} · 在线就绪</span>
        </div>
        <div class="flex items-center space-x-2 text-slate-400">
          <span class="w-2 h-2 rounded-full shrink-0" :class="systemStatus.ossStatus === 'online' ? 'bg-emerald-500' : 'bg-rose-500'"></span>
          <span class="truncate">阿里云 OSS · {{ systemStatus.ossStatus === 'online' ? '正常挂载' : '未挂载' }}</span>
        </div>
      </div>

    </aside>

    <!-- 右侧主应用视窗 (全屏通透空气感画布) -->
    <main class="flex-1 h-screen overflow-hidden flex flex-col relative z-10">
      <!-- 动态浅色高阶星轨画布背景 -->
      <SciFiBackground />

      <!-- 页面视图渲染 -->
      <div class="flex-1 h-full overflow-hidden">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useChatStore } from './stores/chat';
import { systemApi } from './api/system';
import SciFiBackground from './components/retrieval/SciFiBackground.vue';

const route = useRoute();
const router = useRouter();
const chatStore = useChatStore();

const showSettingsModal = ref(false);

const systemStatus = ref({
  modelName: '',
  dbStatus: 'online',
  ossStatus: 'online'
});

onMounted(async () => {
  try {
    const health = await systemApi.getHealth();
    if (health) {
      systemStatus.value = {
        modelName: health.model_name || '',
        dbStatus: health.db_status || 'offline',
        ossStatus: health.oss_status || 'unconfigured'
      };
    }
  } catch (err) {
    console.error('获取系统组件监控状态失败:', err);
  }
});

const handleNewChat = () => {
  chatStore.createNewSession();
  if (route.path !== '/retrieval') {
    router.push('/retrieval');
  }
};

const handleSelectSession = (id: string) => {
  chatStore.switchSession(id);
  if (route.path !== '/retrieval') {
    router.push('/retrieval');
  }
};

const handleDeleteSession = (id: string) => {
  if (confirm('确认删除此问答会话吗？')) {
    chatStore.deleteSession(id);
  }
};

const formatTime = (ts: number) => {
  const d = new Date(ts);
  const m = (d.getMonth() + 1).toString().padStart(2, '0');
  const day = d.getDate().toString().padStart(2, '0');
  const h = d.getHours().toString().padStart(2, '0');
  const min = d.getMinutes().toString().padStart(2, '0');
  return `${m}/${day} ${h}:${min}`;
};
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
