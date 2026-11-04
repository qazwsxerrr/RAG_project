<template>
  <div class="retrieval-chat-view h-full flex flex-col overflow-hidden relative select-none">
    
    <!-- 顶部极简顶栏 (专业平铺工控风格，杜绝大框套小框与文字折行) -->
    <header class="h-13 border-b border-slate-200/80 bg-white px-5 flex items-center justify-between z-10 gap-3">
      <!-- 左侧：页面标题与状态 -->
      <div class="flex items-center space-x-3 shrink-0 min-w-0">
        <h2 class="text-sm font-bold text-slate-900 tracking-tight truncate max-w-[160px] md:max-w-[220px]" :title="chatStore.currentSession?.title">
          {{ chatStore.currentSession?.title || '知识问答' }}
        </h2>
        <span class="hidden sm:inline-flex items-center gap-1.5 text-[11px] font-mono text-slate-400 shrink-0">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
          <span>在线</span>
        </span>
      </div>

      <!-- 右侧控制区：统一纯平铺下拉与分段控制器 -->
      <div class="flex items-center space-x-2 text-xs shrink-0 whitespace-nowrap overflow-x-auto py-1">
        <!-- 当前登录员工身份与可见部门范围展示 (只读回显，支持真实 SSO 切换账号) -->
        <div class="flex items-center gap-1.5 shrink-0 whitespace-nowrap bg-slate-100 px-2 py-1 rounded-md border border-slate-200/80">
          <span class="text-slate-500 font-medium text-xs select-none">当前员工:</span>
          <span class="text-slate-800 font-semibold text-xs">
            {{ authStore.currentUser?.username || '已登录员工' }}
          </span>
          <span class="text-slate-400 text-xs">|</span>
          <span class="text-indigo-600 font-medium text-xs">
            {{ authStore.currentUser?.dept_path === '/' ? '全域 (/)' : (authStore.currentUser?.dept_path || '全域') }}
          </span>
          <button
            @click="authStore.logout()"
            class="ml-1 px-1.5 py-0.5 rounded bg-white hover:bg-rose-50 text-slate-600 hover:text-rose-600 border border-slate-200/80 hover:border-rose-200 text-[11px] font-medium transition-colors cursor-pointer"
            title="退出当前员工会话并切换 SSO 账号"
          >
            切换账号
          </button>
        </div>

        <div class="h-4 w-px bg-slate-200 shrink-0"></div>

        <!-- 部门范围选择器 (纯平铺，杜绝套框) -->
        <div class="flex items-center gap-1.5 shrink-0 whitespace-nowrap">
          <span class="text-slate-500 font-medium text-xs select-none">部门:</span>
          <select 
            v-model="selectedDeptPath"
            class="h-7 px-2 bg-slate-100 hover:bg-slate-200/70 text-slate-800 font-medium text-xs rounded-md outline-none border border-slate-200/80 cursor-pointer transition-colors"
            title="限定检索部门范围"
          >
            <option value="">{{ authStore.currentUser?.dept_path === '/' ? '全部部门 (全域)' : '本部门默认范围' }}</option>
            <option v-for="d in departmentList" :key="d.id" :value="d.path">
              {{ isDeptAllowed(d.path) ? d.name : `${d.name} (跨部门·受保护)` }}
            </option>
          </select>
        </div>

        <div class="h-4 w-px bg-slate-200 shrink-0"></div>

        <!-- 分类选择器 (纯平铺，杜绝套框) -->
        <div class="flex items-center gap-1.5 shrink-0 whitespace-nowrap">
          <span class="text-slate-500 font-medium text-xs select-none">分类:</span>
          <select 
            v-model="selectedCategory"
            class="h-7 px-2 bg-slate-100 hover:bg-slate-200/70 text-slate-800 font-medium text-xs rounded-md outline-none border border-slate-200/80 cursor-pointer transition-colors"
            title="前置过滤知识分类"
          >
            <option v-for="c in categoryList" :key="c" :value="c">
              {{ c }}
            </option>
          </select>
        </div>

        <div class="h-4 w-px bg-slate-200 shrink-0"></div>

        <!-- 检索场景分段控制组 -->
        <div class="inline-flex p-0.5 bg-slate-100 rounded-lg text-xs font-medium shrink-0">
          <button 
            v-for="s in sceneOptions" 
            :key="s.value"
            @click="sceneType = s.value"
            class="px-2 py-1 rounded-md transition-all text-xs"
            :class="sceneType === s.value ? 'bg-white text-slate-900 shadow-xs font-medium' : 'text-slate-500 hover:text-slate-800'"
          >
            {{ s.label }}
          </button>
        </div>

        <div class="h-4 w-px bg-slate-200 shrink-0"></div>

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

        <!-- 清空对话 -->
        <button 
          @click="chatStore.clearCurrentMessages" 
          class="text-slate-400 hover:text-slate-700 p-1.5 rounded-lg hover:bg-slate-100 transition-colors shrink-0"
          title="清空当前会话消息"
        >
          <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
        </button>
      </div>
    </header>

    <!-- 中部消息流展示区 -->
    <div 
      ref="messageContainerRef"
      class="flex-1 overflow-y-auto px-4 sm:px-8 py-6 space-y-6 scroll-smooth select-text bg-[#FAFBFD]"
    >
      <div class="max-w-3xl mx-auto w-full pb-28">
        <!-- 越权拦截全局浮动警示横幅 (403 Forbidden 醒目弹窗) -->
        <div 
          v-if="authStore.interceptAlert"
          class="mb-6 p-4 bg-rose-50/95 border-2 border-rose-400 rounded-xl flex items-start gap-3.5 shadow-lg animate-in fade-in slide-in-from-top-3 duration-300"
        >
          <div class="text-rose-600 font-bold text-2xl leading-none mt-0.5 select-none">🛡️</div>
          <div class="flex-1 text-xs">
            <div class="font-bold text-rose-800 text-sm flex items-center gap-2">
              <span>BOLA / IDOR 越权访问物理拦截</span>
              <span class="px-2 py-0.5 rounded bg-rose-200 text-rose-900 text-[10px] font-mono font-bold">403 Forbidden</span>
            </div>
            <div class="text-rose-800 font-mono text-xs mt-1.5 bg-white/90 p-2.5 rounded-lg border border-rose-300 leading-relaxed break-all">
              ❌ {{ authStore.interceptAlert }}
            </div>
            <div class="text-[11px] text-rose-700 mt-2 flex items-center gap-1 font-medium">
              <span>💡 当前员工为 <b>{{ authStore.currentUser?.username || '已登录员工' }}</b>。若需跨部门检索，请联系企业系统管理员调整权限！</span>
            </div>
          </div>
          <button 
            @click="authStore.interceptAlert = ''" 
            class="text-rose-400 hover:text-rose-700 text-lg font-bold p-1 rounded hover:bg-rose-100 transition-colors"
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
          <span class="text-xs text-slate-400 font-medium">正在拉取云端对话历史...</span>
        </div>

        <!-- 空状态欢迎区域 (去除所有彩色大卡片与莫名其妙套框) -->
        <div 
          v-else-if="!chatStore.currentSession || chatStore.currentSession.messages.length === 0"
          class="min-h-[48vh] flex flex-col items-center justify-center text-center max-w-lg mx-auto space-y-5 animate-in fade-in duration-300 select-none"
        >
          <div class="w-11 h-11 rounded-xl bg-slate-100 border border-slate-200 flex items-center justify-center text-slate-700">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>
          </div>
          <div class="space-y-1">
            <h3 class="text-sm font-bold text-slate-800">企业级多模态知识问答</h3>
            <p class="text-xs text-slate-400 leading-relaxed max-w-sm">
              支持多路高维稠密检索、倒排索引与 BGE 精排打分，问答过程支持阶段推演流与切片精准溯源。
            </p>
          </div>

          <!-- 推荐样例提问 (清爽灰色微圆角胶囊) -->
          <div class="flex flex-wrap items-center justify-center gap-2 pt-1">
            <button 
              v-for="prompt in samplePrompts" 
              :key="prompt"
              @click="sendPresetPrompt(prompt)"
              class="text-xs text-slate-600 bg-white hover:bg-slate-50 hover:text-slate-900 border border-slate-200/90 px-3 py-1.5 rounded-lg transition-colors text-left"
            >
              {{ prompt }}
            </button>
          </div>
        </div>

        <!-- 消息列表渲染 (对话自然呼吸于画布之上) -->
        <ChatMessage 
          v-for="msg in chatStore.currentSession?.messages || []"
          :key="msg.id"
          :message="msg"
          @citationClick="handleCitationClick"
        />
      </div>
    </div>

    <!-- 底部悬浮极简输入框 -->
    <div class="absolute bottom-6 left-0 right-0 z-20 flex justify-center px-4 pointer-events-none">
      <div class="pointer-events-auto max-w-2xl w-full bg-white rounded-xl shadow-lg border border-slate-200 p-2 pl-3.5 flex items-center gap-2 transition-all focus-within:border-slate-400">
        <textarea 
          ref="inputRef"
          v-model="inputQuery"
          @keydown.enter.exact.prevent="handleSend"
          rows="1"
          placeholder="输入问题，回车发送 (Shift + Enter 换行)..."
          class="w-full text-xs sm:text-sm bg-transparent text-slate-800 placeholder-slate-400 outline-none resize-none max-h-28 leading-relaxed select-text"
          :disabled="chatStore.isGenerating"
        ></textarea>

        <!-- 终止生成按钮 -->
        <button 
          v-if="chatStore.isGenerating"
          @click="chatStore.stopGeneration"
          class="w-8 h-8 rounded-lg bg-rose-600 hover:bg-rose-700 text-white flex items-center justify-center transition-colors flex-shrink-0"
          title="中止生成"
        >
          <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
        </button>

        <!-- 简洁发送按钮 -->
        <button 
          v-else
          @click="handleSend"
          :disabled="!inputQuery.trim()"
          class="w-8 h-8 rounded-lg bg-slate-900 hover:bg-slate-800 text-white flex items-center justify-center transition-colors flex-shrink-0 disabled:opacity-30 disabled:cursor-not-allowed"
          title="发送"
        >
          <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="22" y1="2" x2="11" y2="13"/>
            <polygon points="22 2 15 22 11 13 2 9 22 2"/>
          </svg>
        </button>
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
import { ref, watch, nextTick, onMounted } from 'vue';
import { useChatStore } from '../stores/chat';
import { useAuthStore } from '../stores/auth';
import { documentsApi } from '../api/documents';
import type { CitationSource, DepartmentItem, DocumentItem } from '../types';
import ChatMessage from '../components/retrieval/ChatMessage.vue';
import CitationModal from '../components/retrieval/CitationModal.vue';

const chatStore = useChatStore();
const authStore = useAuthStore();

const inputQuery = ref('');
const inputRef = ref<HTMLTextAreaElement | null>(null);
const messageContainerRef = ref<HTMLElement | null>(null);

const sceneType = ref('general');
const enableHyde = ref(false);

const selectedDeptPath = ref('');
const selectedCategory = ref('全部');
const departmentList = ref<DepartmentItem[]>([]);
const categoryList = ref<string[]>(['全部', '技术规范', '操作规程', '产品文档', '行业报告', '面试题']);

const isDeptAllowed = (deptPath: string): boolean => {
  const u = authStore.currentUser;
  if (!u) return true;
  if (u.data_scope === 'ALL' || u.dept_path === '/') return true;
  const prefix = u.dept_path.replace(/\/+$/, '');
  const target = deptPath.replace(/\/+$/, '');
  return target === prefix || target.startsWith(prefix + '/');
};

onMounted(async () => {
  try {
    await authStore.initAuth();
    if (authStore.currentUser?.dept_path && authStore.currentUser.dept_path !== '/') {
      selectedDeptPath.value = authStore.currentUser.dept_path;
    }
  } catch (err) {
    console.error('初始化员工身份失败:', err);
  }

  try {
    await chatStore.initSessions();
  } catch (err) {
    console.error('初始化云端会话列表失败:', err);
  }

  try {
    const depts = await documentsApi.getDepartments();
    departmentList.value = depts;
  } catch (err) {
    console.error('加载部门列表失败:', err);
  }

  try {
    const docs = await documentsApi.getDocuments();
    const catSet = new Set(categoryList.value);
    docs.forEach((d: DocumentItem) => {
      if (d.category) catSet.add(d.category);
    });
    categoryList.value = Array.from(catSet);
  } catch (err) {
    console.error('加载分类列表失败:', err);
  }
});

const sceneOptions = [
  { value: 'general', label: '智能综合' },
  { value: 'compliance', label: '精准条款' },
  { value: 'technical', label: '深度原理' },
];

const citationModalVisible = ref(false);
const activeCitation = ref<CitationSource | null>(null);

const samplePrompts = [
  'RAG和Fine-tuning(微调)区别是啥呀',
  'rag演进本质是啥',
  '中国AI行业大模型典型案例有哪些',
  '大模型产业发展报告的主要结论是什么？',
];

const scrollToBottom = async () => {
  await nextTick();
  if (messageContainerRef.value) {
    messageContainerRef.value.scrollTop = messageContainerRef.value.scrollHeight;
  }
};

const handleSend = async () => {
  const q = inputQuery.value.trim();
  if (!q || chatStore.isGenerating) return;

  authStore.interceptAlert = '';
  inputQuery.value = '';
  scrollToBottom();

  await chatStore.sendMessage(q, {
    scene_type: sceneType.value,
    enable_hyde: enableHyde.value,
    department_scope: selectedDeptPath.value ? [selectedDeptPath.value] : undefined,
    category_scope: selectedCategory.value !== '全部' ? selectedCategory.value : undefined,
  });

  scrollToBottom();
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
  () => chatStore.currentSession?.messages,
  () => {
    scrollToBottom();
  },
  { deep: true }
);
</script>
