<template>
  <div class="retrieval-chat-view h-full flex flex-col overflow-hidden relative select-none">
    <!-- 顶部控制栏：左侧会话标题，右侧平铺部门/分类/场景分段/HyDE 假想开关与删除会话 -->
    <header class="min-h-14 border-b border-slate-200/80 bg-white px-4 sm:px-5 py-1.5 flex flex-wrap items-center justify-between shrink-0 z-10 gap-x-3 gap-y-1.5">
      <div class="flex items-center space-x-2.5 min-w-0 shrink-0">
        <button
          v-if="openMobileMenu"
          @click="openMobileMenu()"
          class="lg:hidden p-1.5 -ml-1 rounded-lg text-slate-600 hover:bg-slate-100 transition-colors cursor-pointer shrink-0"
          title="打开导航菜单"
        >
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
        </button>
        <h2 class="text-sm font-bold text-slate-900 tracking-tight truncate max-w-[160px] sm:max-w-[220px]" :title="chatStore.currentSession?.title">
          {{ chatStore.currentSession?.title || '知识问答' }}
        </h2>
        <span class="hidden sm:inline-flex items-center gap-1.5 text-[11px] font-mono text-slate-400 shrink-0">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
          <span>在线</span>
        </span>
      </div>

      <!-- 右侧控制区：恢复顶部平铺部门、分类、检索场景与 HyDE 假想开关（移除重复的员工胶囊以保证笔记本视口不拥挤） -->
      <div class="flex items-center flex-wrap sm:flex-nowrap gap-2 text-xs overflow-x-auto max-w-full py-0.5">
        <!-- 部门范围选择器 -->
        <div class="flex items-center gap-1.5 shrink-0 whitespace-nowrap">
          <span class="text-slate-500 font-medium text-xs select-none">部门:</span>
          <select
            v-model="selectedDeptPath"
            class="h-7 px-2 bg-slate-100 hover:bg-slate-200/70 text-slate-800 font-medium text-xs rounded-md outline-none border border-slate-200/80 cursor-pointer transition-colors max-w-[140px]"
            title="限定检索部门范围"
          >
            <option value="">{{ isFullScope ? '全部部门' : '授权范围默认' }}</option>
            <option v-for="d in visibleDepartments" :key="d.id" :value="d.path">
              {{ d.name }}
            </option>
          </select>
        </div>

        <div class="hidden sm:block h-4 w-px bg-slate-200 shrink-0"></div>

        <!-- 分类选择器 -->
        <div class="flex items-center gap-1.5 shrink-0 whitespace-nowrap">
          <span class="text-slate-500 font-medium text-xs select-none">分类:</span>
          <select
            v-model="selectedCategory"
            class="h-7 px-2 bg-slate-100 hover:bg-slate-200/70 text-slate-800 font-medium text-xs rounded-md outline-none border border-slate-200/80 cursor-pointer transition-colors max-w-[130px]"
            title="限定文档分类"
          >
            <option v-for="c in categoryList" :key="c" :value="c">
              {{ c }}
            </option>
          </select>
        </div>

        <div class="hidden sm:block h-4 w-px bg-slate-200 shrink-0"></div>

        <!-- 检索场景分段控制组 -->
        <div class="inline-flex p-0.5 bg-slate-100 rounded-lg text-xs font-medium shrink-0">
          <button
            v-for="s in sceneOptions"
            :key="s.value"
            @click="sceneType = s.value"
            class="px-2 py-1 rounded-md transition-all text-xs cursor-pointer"
            :class="sceneType === s.value ? 'bg-white text-slate-900 shadow-sm font-medium' : 'text-slate-500 hover:text-slate-800'"
          >
            {{ s.label }}
          </button>
        </div>

        <div class="hidden sm:block h-4 w-px bg-slate-200 shrink-0"></div>

        <!-- HyDE 假想开关 -->
        <button
          @click="enableHyde = !enableHyde"
          class="inline-flex items-center gap-1.5 px-2 py-1 text-xs font-medium transition-colors cursor-pointer rounded-md hover:bg-slate-50 shrink-0 whitespace-nowrap"
          :class="enableHyde ? 'text-blue-600' : 'text-slate-500 hover:text-slate-700'"
          title="开启后系统将先调用大模型做假想推理再二次召回"
        >
          <span class="w-1.5 h-1.5 rounded-full" :class="enableHyde ? 'bg-blue-600' : 'bg-slate-300'"></span>
          <span>HyDE 假想</span>
        </button>

        <!-- 删除当前会话 -->
        <button
          @click="handleDeleteCurrentSession"
          class="text-slate-400 hover:text-rose-600 p-1.5 rounded-lg hover:bg-slate-100 transition-colors shrink-0 cursor-pointer"
          title="删除当前会话"
        >
          <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
        </button>
      </div>
    </header>

    <!-- 中部消息流展示区 -->
    <div
      ref="messageContainerRef"
      @scroll="handleScroll"
      class="flex-1 overflow-y-auto px-4 sm:px-8 py-6 space-y-6 select-text bg-[#FAFBFD]"
    >
      <div class="max-w-3xl mx-auto w-full pb-4">
        <!-- 权限或频率提示横幅（仅在非对话消息类全局拦截时展示） -->
        <div
          v-if="authStore.interceptAlert"
          class="mb-5 p-3.5 bg-amber-50 border border-amber-200 rounded-xl flex items-start gap-3 shadow-sm"
        >
          <svg class="w-4 h-4 text-amber-600 mt-0.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          <div class="flex-1 text-xs text-amber-900 space-y-1">
            <div class="font-semibold">暂无该部门访问权限或操作受限</div>
            <div class="text-amber-800 leading-relaxed break-all">{{ authStore.interceptAlert }}</div>
          </div>
          <button
            @click="authStore.interceptAlert = ''"
            class="text-amber-500 hover:text-amber-800 text-sm p-1 rounded hover:bg-amber-100 transition-colors cursor-pointer"
            title="关闭提示"
          >
            ✕
          </button>
        </div>

        <!-- 正在加载历史消息状态 -->
        <div
          v-if="chatStore.isLoadingMessages && (!chatStore.currentSession || chatStore.currentSession.messages.length === 0)"
          class="min-h-[40vh] flex flex-col items-center justify-center text-center space-y-3 select-none"
        >
          <svg class="w-6 h-6 animate-spin text-blue-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="10" stroke-opacity="0.25" stroke="currentColor" fill="none"/>
            <path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/>
          </svg>
          <span class="text-xs text-slate-400 font-medium">正在加载对话历史...</span>
        </div>

        <!-- 空状态欢迎区域 -->
        <div
          v-else-if="!chatStore.currentSession || chatStore.currentSession.messages.length === 0"
          class="min-h-[48vh] flex flex-col items-center justify-center text-center max-w-lg mx-auto space-y-5 animate-in fade-in duration-300 select-none"
        >
          <div class="w-11 h-11 rounded-xl bg-slate-100 border border-slate-200 flex items-center justify-center text-slate-700">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>
          </div>
          <div class="space-y-1">
            <h3 class="text-sm font-bold text-slate-800">企业级多模态知识问答</h3>
          </div>

          <!-- 基于当前员工可见文档动态生成的示例提问 -->
          <div v-if="samplePrompts.length > 0" class="flex flex-wrap items-center justify-center gap-2 pt-1">
            <button
              v-for="prompt in samplePrompts"
              :key="prompt"
              @click="sendPresetPrompt(prompt)"
              class="text-xs text-slate-600 bg-white hover:bg-slate-50 hover:text-slate-900 border border-slate-200/90 px-3 py-1.5 rounded-lg transition-colors text-left cursor-pointer"
            >
              {{ prompt }}
            </button>
          </div>
        </div>

        <!-- 消息列表渲染 -->
        <ChatMessage
          v-for="msg in chatStore.currentSession?.messages || []"
          :key="msg.id"
          :message="msg"
          @citationClick="handleCitationClick"
        />
      </div>
    </div>

    <!-- 底部纯净输入栏（Flex 自然置底，不遮挡消息流末尾的参考来源） -->
    <div class="shrink-0 bg-[#FAFBFD] px-4 pb-5 pt-2 z-10">
      <div class="max-w-2xl mx-auto w-full">
        <div class="w-full bg-white rounded-xl shadow-lg border border-slate-200 p-2 pl-3.5 flex items-center gap-2 transition-all focus-within:border-slate-400">
          <textarea
            ref="inputRef"
            v-model="inputQuery"
            @keydown.enter.exact.prevent="handleSend"
            rows="1"
            placeholder="输入问题，回车发送 (Shift + Enter 换行)..."
            class="w-full text-xs sm:text-sm bg-transparent text-slate-800 placeholder-slate-400 outline-none resize-none max-h-28 leading-relaxed select-text"
            :disabled="chatStore.isGenerating"
          ></textarea>

          <button
            v-if="chatStore.isGenerating"
            @click="chatStore.stopGeneration"
            class="w-8 h-8 rounded-lg bg-rose-600 hover:bg-rose-700 text-white flex items-center justify-center transition-colors shrink-0 cursor-pointer"
            title="停止生成"
          >
            <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
          </button>

          <button
            v-else
            @click="handleSend"
            :disabled="!inputQuery.trim()"
            class="w-8 h-8 rounded-lg bg-slate-900 hover:bg-slate-800 text-white flex items-center justify-center transition-colors shrink-0 disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer"
            title="发送"
          >
            <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="22" y1="2" x2="11" y2="13"/>
              <polygon points="22 2 15 22 11 13 2 9 22 2"/>
            </svg>
          </button>
        </div>
      </div>
    </div>

    <!-- 原文精准溯源弹窗 -->
    <CitationModal
      :visible="citationModalVisible"
      :citation="activeCitation"
      @close="citationModalVisible = false"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted, inject } from 'vue';
import { useChatStore } from '../stores/chat';
import { useAuthStore } from '../stores/auth';
import { useDocumentStore } from '../stores/document';
import type { CitationSource, DocumentItem } from '../types';
import ChatMessage from '../components/retrieval/ChatMessage.vue';
import CitationModal from '../components/retrieval/CitationModal.vue';

const chatStore = useChatStore();
const authStore = useAuthStore();
const documentStore = useDocumentStore();
const openMobileMenu = inject<(() => void) | undefined>('openMobileMenu', undefined);

const inputQuery = ref('');
const inputRef = ref<HTMLTextAreaElement | null>(null);
const messageContainerRef = ref<HTMLElement | null>(null);

const sceneType = ref('general');
const enableHyde = ref(false);

const selectedDeptPath = ref('');
const selectedCategory = ref('全部');

const isFullScope = computed(() => {
  const u = authStore.currentUser;
  return Boolean(u && (u.data_scope === 'ALL' || u.dept_path === '/'));
});

const isDeptAllowed = (deptPath: string): boolean => {
  const u = authStore.currentUser;
  if (!u) return true;
  if (u.data_scope === 'ALL' || u.dept_path === '/') return true;
  const target = deptPath.replace(/\/+$/, '') || '/';
  if (target === '/public') return true;
  const prefix = u.dept_path.replace(/\/+$/, '');
  return target === prefix || target.startsWith(prefix + '/');
};

const visibleDepartments = computed(() => {
  return documentStore.departments.filter((d) => d.path && d.path !== '/' && isDeptAllowed(d.path));
});

const categoryList = computed(() => {
  const catSet = new Set<string>(['全部']);
  documentStore.documents.forEach((d: DocumentItem) => {
    if (d.category && d.category.trim()) {
      catSet.add(d.category.trim());
    }
  });
  return Array.from(catSet);
});

onMounted(async () => {
  authStore.interceptAlert = '';
  try {
    await authStore.initAuth();
  } catch (err) {
    console.error('初始化员工身份失败:', err);
  }

  try {
    await chatStore.initSessions();
  } catch (err) {
    console.error('初始化云端会话列表失败:', err);
  }

  if (documentStore.departments.length === 0) {
    await documentStore.fetchDepartments();
  }
  if (documentStore.documents.length === 0) {
    try {
      await documentStore.fetchDocuments();
    } catch (err) {
      console.warn('加载文档分类列表失败:', err);
    }
  }
});

const sceneOptions = [
  { value: 'general', label: '智能综合' },
  { value: 'compliance', label: '精准条款' },
  { value: 'technical', label: '深度原理' },
];

const citationModalVisible = ref(false);
const activeCitation = ref<CitationSource | null>(null);

const samplePrompts = computed(() => {
  const deptPath = authStore.currentUser?.dept_path || '';
  // 针对不同部门员工，提供自然业务检索问题 + 1 条跨部门权限边界验证问题（不点名具体文件、不套书名号）
  if (deptPath.includes('财务')) {
    return [
      '高管年终绩效奖金的分配基准系数与发放流程是什么？',
      '单项研发外包合同超过多少金额需要附带 ROI 测算报告？',
      '大额差旅与业务招待支出有哪些双签核准要求？',
      '微服务网关熔断器的超时阈值和恢复窗口是多少？（跨部门权限验证）',
    ];
  }
  if (deptPath.includes('技术') || deptPath.includes('研发')) {
    return [
      '微服务网关熔断隔离的触发条件与跨机房容灾 RTO 指标是多少？',
      '新服务上线时金丝雀灰度发布的流量分流比例和观察时长怎么规定？',
      'RAG 架构从传统线性检索到模块化演进的核心阶段有哪些？',
      '2026 年度高管年终绩效奖金分配基准系数是多少？（跨部门权限验证）',
    ];
  }
  return [
    '微服务网关熔断隔离的触发条件与跨机房容灾 RTO 指标是多少？',
    '2026 年度高管年终绩效奖金的分配基准系数与审批流程是什么？',
    'RAG 架构从传统线性检索到模块化演进的核心阶段有哪些？',
    '单项技术研发外包合同达到什么标准需要提交财务 ROI 测算？',
  ];
});

// 智能贴底滚动状态：用户主动上滑阅读前文时不强夺滚动条
const isNearBottom = ref(true);
let scrollRafId: number | null = null;

const handleScroll = () => {
  const el = messageContainerRef.value;
  if (!el) return;
  const distanceToBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
  isNearBottom.value = distanceToBottom <= 80;
};

const scrollToBottom = async (force = false) => {
  if (!force && !isNearBottom.value) return;
  await nextTick();
  if (scrollRafId !== null) return;
  scrollRafId = requestAnimationFrame(() => {
    scrollRafId = null;
    if (messageContainerRef.value && (force || isNearBottom.value)) {
      messageContainerRef.value.scrollTop = messageContainerRef.value.scrollHeight;
    }
  });
};

const handleDeleteCurrentSession = async () => {
  if (!chatStore.currentSessionId) return;
  if (confirm('确认删除当前会话及其历史消息吗？')) {
    await chatStore.clearCurrentMessages();
  }
};

const handleSend = async () => {
  const q = inputQuery.value.trim();
  if (!q || chatStore.isGenerating) return;

  authStore.interceptAlert = '';
  inputQuery.value = '';
  isNearBottom.value = true;
  scrollToBottom(true);

  await chatStore.sendMessage(q, {
    scene_type: sceneType.value,
    enable_hyde: enableHyde.value,
    department_scope: selectedDeptPath.value ? [selectedDeptPath.value] : undefined,
    category_scope: selectedCategory.value !== '全部' ? selectedCategory.value : undefined,
  });

  scrollToBottom(false);
};

const sendPresetPrompt = (prompt: string) => {
  inputQuery.value = prompt;
  handleSend();
};

const handleCitationClick = (cit: CitationSource) => {
  activeCitation.value = cit;
  citationModalVisible.value = true;
};

watch(
  () => chatStore.currentSessionId,
  () => {
    isNearBottom.value = true;
    scrollToBottom(true);
  }
);

watch(
  () => chatStore.currentSession?.messages,
  () => {
    scrollToBottom(false);
  },
  { deep: true }
);

onUnmounted(() => {
  if (scrollRafId !== null) {
    cancelAnimationFrame(scrollRafId);
  }
});
</script>
