<template>
  <div class="retrieval-chat-view h-full flex flex-col overflow-hidden relative select-none">
    <!-- 顶部控制栏：左侧会话标题，右侧知识库多选、分类、场景与 HyDE -->
    <header class="min-h-14 border-b border-slate-200/80 bg-white px-4 sm:px-5 py-1.5 flex flex-wrap items-center justify-between shrink-0 z-20 gap-x-3 gap-y-1.5">
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
          {{ (chatStore.currentSession?.messages?.length ? chatStore.currentSession.title : '') || '知识问答' }}
        </h2>
        <span class="hidden sm:inline-flex items-center gap-1.5 text-[11px] font-mono text-slate-400 shrink-0">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
          <span>在线</span>
        </span>
      </div>

      <!-- 右侧控制区 -->
      <div class="flex items-center flex-wrap sm:flex-nowrap gap-2 text-xs max-w-full py-0.5">
        <!-- 知识库多选器（由 /api/v1/knowledge-bases?action=READ 实时驱动） -->
        <div class="relative shrink-0" ref="kbDropdownRef">
          <button
            type="button"
            @click="kbDropdownOpen = !kbDropdownOpen"
            class="h-7 px-2.5 bg-slate-100 hover:bg-slate-200/70 text-slate-800 font-medium text-xs rounded-md border border-slate-200/80 cursor-pointer transition-colors flex items-center gap-1.5 max-w-[220px]"
            title="选择参与本次问答检索的知识库（不勾选则默认检索全部有权读取的知识库）"
          >
            <svg class="w-3.5 h-3.5 text-slate-500 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
            <span class="truncate">{{ kbSelectorLabel }}</span>
            <svg class="w-3 h-3 text-slate-400 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
          </button>

          <!-- 分组知识库下拉面板 -->
          <div
            v-if="kbDropdownOpen"
            class="absolute left-0 md:left-auto md:right-0 mt-1.5 w-80 max-w-[calc(100vw-2rem)] max-h-96 overflow-y-auto bg-white rounded-xl shadow-xl border border-slate-200 p-2.5 z-50 space-y-2"
          >
            <div class="flex items-center justify-between px-1 pb-1.5 border-b border-slate-100">
              <span class="text-[11px] font-semibold text-slate-600">限定检索知识库（可多选）</span>
              <button
                v-if="selectedKbIds.length > 0"
                type="button"
                @click="selectedKbIds = []"
                class="text-[11px] text-blue-600 hover:text-blue-800 cursor-pointer"
              >
                恢复默认全部
              </button>
            </div>

            <div v-if="readableKbs.length === 0" class="py-4 text-center text-slate-400 text-xs">
              暂无已授权知识库
            </div>

            <template v-for="group in kbGroups" :key="group.key">
              <div v-if="group.items.length > 0" class="space-y-1">
                <div class="px-1 text-[10px] font-bold uppercase tracking-wider text-slate-400">{{ group.label }}</div>
                <label
                  v-for="kb in group.items"
                  :key="kb.id"
                  class="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-slate-50 cursor-pointer transition-colors"
                >
                  <input
                    type="checkbox"
                    :value="kb.id"
                    v-model="selectedKbIds"
                    class="rounded border-slate-300 text-blue-600 focus:ring-0 w-3.5 h-3.5 cursor-pointer shrink-0"
                  />
                  <div class="min-w-0 flex-1">
                    <div class="text-xs font-medium text-slate-800 truncate">{{ kb.name }}</div>
                    <div class="text-[10px] text-slate-400 font-mono truncate">{{ kb.owner_dept_path }}</div>
                  </div>
                </label>
              </div>
            </template>
          </div>
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
        <!-- 权限或频率提示横幅 -->
        <div
          v-if="authStore.interceptAlert"
          class="mb-5 p-3.5 bg-amber-50 border border-amber-200 rounded-xl flex items-start gap-3 shadow-sm"
        >
          <svg class="w-4 h-4 text-amber-600 mt-0.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          <div class="flex-1 text-xs text-amber-900 space-y-1">
            <div class="font-semibold">权限校验拦截提示</div>
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
          <span class="text-xs text-slate-400 font-medium">正在加载已复核的对话历史...</span>
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
            <p v-if="authStore.currentUser" class="text-[11px] text-slate-400">
              当前授权范围：{{ readableKbs.length }} 个知识库 · 密级上限 L{{ authStore.currentUser.clearance_level }}
            </p>
          </div>

          <!-- 示例提问 -->
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

    <!-- 底部输入栏 -->
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
import { useAuthStore, isTokenExpired } from '../stores/auth';
import { useDocumentStore } from '../stores/document';
import { accessControlApi } from '../api/accessControl';
import type { AccessibleKnowledgeBase, CitationSource, DocumentItem } from '../types';
import ChatMessage from '../components/retrieval/ChatMessage.vue';
import CitationModal from '../components/retrieval/CitationModal.vue';

const chatStore = useChatStore();
const authStore = useAuthStore();
const documentStore = useDocumentStore();
const openMobileMenu = inject<(() => void) | undefined>('openMobileMenu', undefined);

const inputQuery = ref('');
const inputRef = ref<HTMLTextAreaElement | null>(null);
const messageContainerRef = ref<HTMLElement | null>(null);
const kbDropdownRef = ref<HTMLElement | null>(null);

const sceneType = ref('general');
const enableHyde = ref(false);

const readableKbs = ref<AccessibleKnowledgeBase[]>([]);
const selectedKbIds = ref<string[]>([]);
const kbDropdownOpen = ref(false);
const selectedCategory = ref('全部');

const kbGroups = computed(() => [
  {
    key: 'PUBLIC',
    label: '公共知识库',
    items: readableKbs.value.filter((k) => k.visibility === 'PUBLIC'),
  },
  {
    key: 'DEPARTMENT',
    label: '部门知识库',
    items: readableKbs.value.filter((k) => k.visibility !== 'PUBLIC'),
  },
]);

const kbSelectorLabel = computed(() => {
  if (selectedKbIds.value.length === 0) {
    return `全部授权库 (${readableKbs.value.length})`;
  }
  if (selectedKbIds.value.length === 1) {
    const found = readableKbs.value.find((k) => k.id === selectedKbIds.value[0]);
    return found ? found.name : '已选 1 个知识库';
  }
  return `已选 ${selectedKbIds.value.length} 个知识库`;
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

const handleDocumentClickOutside = (event: MouseEvent) => {
  if (!kbDropdownOpen.value) return;
  const target = event.target as Node | null;
  if (kbDropdownRef.value && target && !kbDropdownRef.value.contains(target)) {
    kbDropdownOpen.value = false;
  }
};

onMounted(async () => {
  document.addEventListener('click', handleDocumentClickOutside);
  authStore.interceptAlert = '';
  try {
    if (!authStore.token || isTokenExpired(authStore.token)) {
      await authStore.refreshToken();
    }
  } catch (err) {
    console.error('初始化员工身份失败:', err);
  }

  try {
    readableKbs.value = await accessControlApi.listAccessibleKnowledgeBases('READ');
  } catch (err) {
    console.warn('加载授权知识库目录失败:', err);
  }

  try {
    await chatStore.initSessions(true);
  } catch (err) {
    console.error('初始化云端会话列表失败:', err);
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
  const deptPaths = authStore.currentUser?.dept_paths || [];
  const joined = deptPaths.join(',');
  if (joined.includes('财务')) {
    return [
      '公司弹性打卡窗口和一线城市差旅住宿标准是多少？',
      '业财一体化报销凭证自动入账与对账核销规范有哪些要求？',
      '公司差旅报销审批流程及费用发票合规标准是什么？',
    ];
  }
  if (joined.includes('人事') || joined.includes('行政')) {
    return [
      '公司弹性打卡窗口和一线城市差旅住宿标准是多少？',
      '员工考勤请假与加班调休审批有哪些规定？',
      '公司入职引导与日常办公行政支持流程是什么？',
    ];
  }
  if (joined.includes('技术') || joined.includes('研发')) {
    return [
      '公司弹性打卡窗口和一线城市差旅住宿标准是多少？',
      '技术部生产灰度发布窗口和微服务限流阈值有哪些规定？',
      '业财一体化报销凭证自动入账的回调签名算法与对账 SLA 是什么？',
      '技术部年度 H800 算力集群采购预算是多少？',
    ];
  }
  return [
    '公司弹性打卡窗口和一线城市差旅住宿标准是多少？',
    '公司年度重点工作规范与跨部门协作流程有哪些？',
    '员工日常办公制度与合规注意事项有哪些？',
  ];
});

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
  kbDropdownOpen.value = false;
  inputQuery.value = '';
  isNearBottom.value = true;
  scrollToBottom(true);

  await chatStore.sendMessage(q, {
    scene_type: sceneType.value,
    enable_hyde: enableHyde.value,
    kb_ids: selectedKbIds.value.length > 0 ? [...selectedKbIds.value] : undefined,
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
  document.removeEventListener('click', handleDocumentClickOutside);
  if (scrollRafId !== null) {
    cancelAnimationFrame(scrollRafId);
  }
});
</script>
