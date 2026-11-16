<template>
  <div class="app-layout h-screen w-screen flex overflow-hidden text-slate-800 relative selection:bg-indigo-500 selection:text-white bg-[#FAFBFD]">
    <!-- 窄屏移动端抽屉遮罩 -->
    <div
      v-if="isMobileMenuOpen"
      @click="isMobileMenuOpen = false"
      class="fixed inset-0 bg-slate-900/35 backdrop-blur-sm z-30 lg:hidden transition-opacity"
    ></div>

    <!-- 左侧全局导航侧边栏：桌面端支持收起，窄屏(<1024px)自动转为滑动抽屉 -->
    <aside
      class="h-screen bg-white border-r border-slate-200/80 flex flex-col justify-between shrink-0 select-none z-40 transition-all duration-200 fixed inset-y-0 left-0 lg:static"
      :class="[
        isMobileMenuOpen ? 'translate-x-0 w-60 shadow-2xl' : '-translate-x-full lg:translate-x-0',
        isSidebarCollapsed ? 'lg:w-16' : 'lg:w-56'
      ]"
    >
      <div class="flex-1 flex flex-col overflow-hidden">
        <!-- 侧边栏顶部：应用品牌 -->
        <div
          class="h-14 px-3.5 flex items-center border-b border-slate-100"
          :class="isSidebarCollapsed ? 'lg:justify-center justify-between' : 'justify-between'"
        >
          <div v-show="!isSidebarCollapsed || isMobileMenuOpen" class="flex items-center space-x-2.5 min-w-0">
            <div class="w-7 h-7 rounded-lg bg-blue-600 flex items-center justify-center text-white shrink-0">
              <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
                <polygon points="12 2 2 7 12 12 22 7 12 2"/>
                <polyline points="2 17 12 22 22 17"/>
                <polyline points="2 12 12 17 22 12"/>
              </svg>
            </div>
            <h1 class="text-sm font-bold text-slate-900 tracking-tight truncate">企业知识库</h1>
          </div>

          <!-- 桌面端折叠/展开按钮 -->
          <button
            @click="isSidebarCollapsed = !isSidebarCollapsed"
            class="hidden lg:flex text-slate-400 hover:text-slate-700 p-1.5 rounded-lg hover:bg-slate-100 transition-colors cursor-pointer"
            :title="isSidebarCollapsed ? '展开侧边栏' : '收起侧边栏'"
          >
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
              <line x1="9" y1="3" x2="9" y2="21"/>
            </svg>
          </button>

          <!-- 窄屏关闭抽屉按钮 -->
          <button
            @click="isMobileMenuOpen = false"
            class="lg:hidden text-slate-400 hover:text-slate-700 p-1.5 rounded-lg hover:bg-slate-100 transition-colors cursor-pointer"
            title="关闭导航"
          >
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>

        <!-- 导航分组 1：功能菜单 -->
        <div class="px-2.5 pt-3 pb-1 space-y-0.5">
          <div v-show="!isSidebarCollapsed || isMobileMenuOpen" class="px-2 py-1 text-[11px] font-semibold text-slate-400">
            功能导航
          </div>

          <router-link
            to="/retrieval"
            @click="isMobileMenuOpen = false"
            class="flex items-center px-2.5 py-2 rounded-lg text-xs transition-colors"
            :class="[
              isSidebarCollapsed && !isMobileMenuOpen ? 'justify-center' : 'space-x-2.5',
              route.path === '/retrieval' ? 'bg-blue-50 text-blue-600 font-semibold' : 'text-slate-600 hover:bg-slate-100/70 hover:text-slate-900'
            ]"
            title="智能问答"
          >
            <svg class="w-4 h-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
            <span v-show="!isSidebarCollapsed || isMobileMenuOpen">智能问答</span>
          </router-link>

          <router-link
            to="/documents"
            @click="isMobileMenuOpen = false"
            class="flex items-center px-2.5 py-2 rounded-lg text-xs transition-colors"
            :class="[
              isSidebarCollapsed && !isMobileMenuOpen ? 'justify-center' : 'space-x-2.5',
              route.path === '/documents' ? 'bg-blue-50 text-blue-600 font-semibold' : 'text-slate-600 hover:bg-slate-100/70 hover:text-slate-900'
            ]"
            title="文档列表"
          >
            <svg class="w-4 h-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
            <span v-show="!isSidebarCollapsed || isMobileMenuOpen">文档列表</span>
          </router-link>

          <router-link
            v-if="authStore.hasPermission(PERMISSIONS.DOC_UPLOAD)"
            to="/documents/new"
            @click="isMobileMenuOpen = false"
            class="flex items-center px-2.5 py-2 rounded-lg text-xs transition-colors"
            :class="[
              isSidebarCollapsed && !isMobileMenuOpen ? 'justify-center' : 'space-x-2.5',
              route.path.startsWith('/documents/new') ? 'bg-blue-50 text-blue-600 font-semibold' : 'text-slate-600 hover:bg-slate-100/70 hover:text-slate-900'
            ]"
            title="提交入库"
          >
            <svg class="w-4 h-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
            <span v-show="!isSidebarCollapsed || isMobileMenuOpen">提交入库</span>
          </router-link>

          <router-link
            to="/channels"
            @click="isMobileMenuOpen = false"
            class="flex items-center px-2.5 py-2 rounded-lg text-xs transition-colors"
            :class="[
              isSidebarCollapsed && !isMobileMenuOpen ? 'justify-center' : 'space-x-2.5',
              route.path === '/channels' ? 'bg-blue-50 text-blue-600 font-semibold' : 'text-slate-600 hover:bg-slate-100/70 hover:text-slate-900'
            ]"
            title="渠道状态"
          >
            <svg class="w-4 h-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
            <span v-show="!isSidebarCollapsed || isMobileMenuOpen">渠道状态</span>
          </router-link>
        </div>

        <!-- 导航分组 2：问答会话管理 -->
        <div v-if="route.path === '/retrieval' && (!isSidebarCollapsed || isMobileMenuOpen)" class="flex-1 flex flex-col min-h-0 px-2.5 pt-3">
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
              class="group px-2.5 py-1.5 rounded-lg cursor-pointer transition-colors text-xs flex items-center justify-between gap-1"
              :class="[
                chatStore.currentSessionId === session.id
                  ? 'bg-slate-100 text-slate-900 font-medium'
                  : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
              ]"
            >
              <input
                v-if="editingSessionId === session.id"
                :ref="setRenameInputRef"
                v-model="editingSessionTitle"
                @click.stop
                @keydown.enter.prevent="commitRename(session.id)"
                @keydown.esc.prevent="cancelRename"
                @blur="commitRename(session.id)"
                class="flex-1 min-w-0 bg-white border border-blue-400 rounded px-1.5 py-0.5 text-xs text-slate-800 outline-none"
              />
              <span
                v-else
                @dblclick.stop="startRename(session.id, session.title)"
                class="truncate flex-1"
                :title="session.title"
              >
                {{ session.title }}
              </span>

              <div v-if="editingSessionId !== session.id" class="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                <button
                  @click.stop="startRename(session.id, session.title)"
                  class="text-slate-400 hover:text-blue-600 p-0.5 rounded"
                  title="重命名会话"
                >
                  <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                </button>
                <button
                  @click.stop="handleDeleteSession(session.id)"
                  class="text-slate-400 hover:text-rose-600 p-0.5 rounded"
                  title="删除会话"
                >
                  <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
                </button>
              </div>
            </div>
          </div>
        </div>

        <div v-else class="flex-1"></div>
      </div>

      <!-- 侧边栏底部：当前登录员工身份卡片（双行排布，完整展示用户名与部门，移除底层组件铭牌） -->
      <div v-if="authStore.currentUser" class="border-t border-slate-100 bg-slate-50/60 p-3">
        <div v-if="!isSidebarCollapsed || isMobileMenuOpen" class="space-y-2">
          <div class="flex items-center justify-between gap-1.5">
            <div class="flex items-center gap-1.5 min-w-0">
              <span class="w-2 h-2 rounded-full bg-indigo-500 shrink-0"></span>
              <span class="text-xs font-bold text-slate-800 truncate">{{ authStore.currentUser.username }}</span>
            </div>
            <span class="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-600 text-[10px] font-mono shrink-0">
              {{ authStore.currentUser.roles?.[0] || 'employee' }}
            </span>
          </div>
          <div class="flex items-center justify-between gap-2">
            <span class="text-[11px] text-slate-500 truncate" :title="authStore.currentUser.dept_path">
              {{ formatDeptShort(authStore.currentUser.dept_path) }}
            </span>
            <button
              @click="authStore.logout()"
              class="px-2 py-0.5 rounded text-[11px] font-medium text-slate-600 hover:text-rose-600 hover:bg-rose-50 border border-slate-200/80 hover:border-rose-200 transition-colors shrink-0 cursor-pointer"
              title="退出当前账号并切换登录身份"
            >
              切换账号
            </button>
          </div>
        </div>

        <div v-else class="flex flex-col items-center gap-2">
          <div
            class="w-7 h-7 rounded-full bg-indigo-50 text-indigo-600 text-xs font-bold flex items-center justify-center uppercase"
            :title="`${authStore.currentUser.username} (${authStore.currentUser.dept_path})`"
          >
            {{ authStore.currentUser.username.slice(0, 1) }}
          </div>
          <button
            @click="authStore.logout()"
            class="p-1.5 rounded-lg text-slate-400 hover:text-rose-600 hover:bg-rose-50 transition-colors cursor-pointer"
            title="切换账号"
          >
            <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
          </button>
        </div>
      </div>
    </aside>

    <!-- 右侧主应用视窗 -->
    <main class="flex-1 min-w-0 h-screen overflow-hidden flex flex-col relative z-10">
      <SciFiBackground />

      <!-- 窄屏(<1024px)非问答页公共轻顶栏（智能问答页已在自身顶栏内嵌汉堡按钮） -->
      <div
        v-if="route.path !== '/retrieval' && route.path !== '/auth/callback'"
        class="lg:hidden h-12 px-4 bg-white border-b border-slate-200/80 flex items-center justify-between shrink-0 z-20"
      >
        <button
          @click="isMobileMenuOpen = true"
          class="p-1.5 -ml-1 rounded-lg text-slate-600 hover:bg-slate-100 transition-colors cursor-pointer"
          title="打开菜单"
        >
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
        </button>
        <span class="text-xs font-bold text-slate-800">{{ route.meta.title || '企业知识库' }}</span>
        <div class="w-6"></div>
      </div>

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
import { ref, provide, nextTick } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useChatStore } from './stores/chat';
import { useAuthStore, PERMISSIONS } from './stores/auth';
import SciFiBackground from './components/retrieval/SciFiBackground.vue';

const route = useRoute();
const router = useRouter();
const chatStore = useChatStore();
const authStore = useAuthStore();

const isSidebarCollapsed = ref(false);
const isMobileMenuOpen = ref(false);
provide('openMobileMenu', () => {
  isMobileMenuOpen.value = true;
});

const editingSessionId = ref<string>('');
const editingSessionTitle = ref<string>('');
const renameInputEl = ref<HTMLInputElement | null>(null);

const setRenameInputRef = (el: unknown) => {
  renameInputEl.value = el instanceof HTMLInputElement ? el : null;
};

const formatDeptShort = (deptPath?: string): string => {
  if (!deptPath || deptPath === '/') return '全部部门';
  const parts = deptPath.split('/').filter(Boolean);
  return parts[parts.length - 1] || deptPath;
};

const handleNewChat = () => {
  chatStore.createNewSession();
  isMobileMenuOpen.value = false;
  if (route.path !== '/retrieval') {
    router.push('/retrieval');
  }
};

const handleSelectSession = (id: string) => {
  chatStore.switchSession(id);
  isMobileMenuOpen.value = false;
  if (route.path !== '/retrieval') {
    router.push('/retrieval');
  }
};

const startRename = async (id: string, currentTitle: string) => {
  editingSessionId.value = id;
  editingSessionTitle.value = currentTitle;
  await nextTick();
  if (renameInputEl.value) {
    renameInputEl.value.focus();
    renameInputEl.value.select();
  }
};

const cancelRename = () => {
  editingSessionId.value = '';
  editingSessionTitle.value = '';
};

const commitRename = async (id: string) => {
  if (editingSessionId.value !== id) return;
  const nextTitle = editingSessionTitle.value.trim();
  cancelRename();
  if (!nextTitle) return;
  try {
    await chatStore.renameSession(id, nextTitle);
  } catch (err: any) {
    alert(`重命名会话失败: ${err.message || '请稍后重试'}`);
  }
};

const handleDeleteSession = (id: string) => {
  if (confirm('确认删除此问答会话吗？')) {
    chatStore.deleteSession(id);
  }
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
