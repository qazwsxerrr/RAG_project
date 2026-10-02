<template>
  <div class="chat-message-item mb-7 transition-all">
    <!-- 用户提问 (轻量自然灰底气泡，避免沉重黑块) -->
    <div v-if="message.role === 'user'" class="flex justify-end">
      <div class="max-w-xl bg-slate-100 text-slate-800 text-xs sm:text-sm px-3.5 py-2 rounded-xl leading-relaxed whitespace-pre-wrap select-text">
        {{ message.content }}
      </div>
    </div>

    <!-- AI 响应内容 (靠左对齐，完全开放呼吸于画布之上) -->
    <div v-else class="flex justify-start">
      <div class="w-full space-y-3">
        
        <!-- 思考流看板嵌入 (清爽无冗余边框) -->
        <ThoughtTimeline v-if="message.thinking" :thinking="message.thinking" />

        <!-- 大模型深度推理可折叠看板 (类似检索流水线规范设计) -->
        <div v-if="hasReasoning" class="deep-thinking-section mb-2 select-none">
          <!-- 顶栏触发器 -->
          <div 
            @click="isReasoningExpanded = !isReasoningExpanded"
            class="py-1 flex items-center justify-between cursor-pointer text-xs text-slate-500 hover:text-slate-800 transition-colors"
          >
            <div class="flex items-center space-x-2">
              <span v-if="message.isReasoning" class="flex h-1.5 w-1.5 relative">
                <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-purple-400 opacity-75"></span>
                <span class="relative inline-flex rounded-full h-1.5 w-1.5 bg-purple-600"></span>
              </span>
              <span v-else class="h-1.5 w-1.5 rounded-full bg-purple-400"></span>

              <span class="font-medium text-slate-700">深度推理过程</span>

              <span v-if="message.isReasoning" class="text-purple-600 font-mono text-[11px] animate-pulse">
                正在深度思考与证据核对中...
              </span>
              <span v-else-if="reasoningDurationText" class="text-slate-400 font-mono text-[11px]">
                {{ reasoningDurationText }}
              </span>
            </div>

            <!-- 右侧仅保留箭头图标，去除汉字提示 -->
            <div 
              class="p-1 rounded text-slate-400 hover:text-slate-700 transition-colors"
              :title="isReasoningExpanded ? '收起思考' : '展开思考'"
            >
              <svg 
                class="w-3.5 h-3.5 transition-transform duration-200" 
                :class="{ 'rotate-180': isReasoningExpanded }" 
                viewBox="0 0 24 24" 
                fill="none" 
                stroke="currentColor" 
                stroke-width="2"
              >
                <polyline points="6 9 12 15 18 9"/>
              </svg>
            </div>
          </div>

          <!-- 抽屉内容区 (纯文本展示，不加框，轻量左侧细线) -->
          <div 
            v-show="isReasoningExpanded" 
            class="mt-1 pl-3.5 py-1 text-xs text-slate-500 leading-relaxed font-sans whitespace-pre-wrap select-text max-h-72 overflow-y-auto border-l-2 border-slate-200"
          >
            {{ cleanReasoningContent }}
          </div>
        </div>

        <!-- 错误提示卡片 -->
        <div v-if="message.error" class="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700 flex items-start gap-2">
          <svg class="w-4 h-4 text-rose-500 mt-0.5 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          <div class="space-y-0.5">
            <div class="font-semibold text-rose-800">推理检索发生异常</div>
            <div class="font-mono text-[11px] text-rose-600">{{ message.error }}</div>
          </div>
        </div>

        <!-- 正文 Markdown 渲染区域 (仅在实际有文本内容时渲染，避免未出字前孤立出现占位) -->
        <div v-if="hasCleanContent" class="relative group pt-1">
          <!-- 正文渲染与角标点击捕获 -->
          <div 
            ref="contentRef"
            @click="handleContentClick"
            class="markdown-body select-text text-slate-800 text-xs sm:text-sm leading-relaxed"
            v-html="renderedMarkdown"
          ></div>

          <!-- 底部轻量辅助条：字数统计与复制回答 -->
          <div v-if="!message.isStreaming && hasCleanContent" class="mt-2.5 pt-2 flex items-center justify-between text-[11px] text-slate-400 select-none">
            <span class="font-mono text-[10px] text-slate-400">
              {{ cleanContentText.length }} 字 · {{ citedIds.size > 0 ? `已标注 ${citedIds.size} 处溯源依据` : '知识库无直接关联依据 (防幻觉保护)' }}
            </span>
            
            <button 
              @click="copyContent"
              class="hover:text-slate-700 flex items-center gap-1 transition-colors px-2 py-0.5 rounded text-slate-400 hover:bg-slate-100 text-[11px]"
              title="复制回答"
            >
              <svg v-if="copied" class="w-3 h-3 text-emerald-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
              <svg v-else class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              <span>{{ copied ? '已复制' : '复制' }}</span>
            </button>
          </div>
        </div>

        <!-- 流式等待大模型生成时的状态提示 -->
        <div v-if="message.isStreaming && !hasCleanContent && !message.isReasoning" class="text-xs text-slate-500 flex items-center gap-2 py-1 select-none">
          <svg class="w-3.5 h-3.5 text-slate-500 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
            <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
          </svg>
          <span v-if="message.citations && message.citations.length > 0" class="font-mono text-[11px] text-slate-500">已锁定 {{ message.citations.length }} 处切片依据 · 大模型正在流式撰写解答...</span>
          <span v-else class="font-mono text-[11px] text-slate-500">多路检索与逻辑推演中，正在锁定知识依据...</span>
        </div>

        <!-- 引用来源卡片条：仅在正文中确实引用了该角标时展示，避免“回答未找到依据却列出参考来源”的矛盾 -->
        <div v-if="displayCitations.length > 0 && (message.content || !message.isStreaming)" class="flex flex-wrap items-center gap-2 pt-2 border-t border-slate-100 text-xs">
          <span class="text-[11px] text-slate-400 font-medium">参考来源：</span>
          <button
            v-for="cit in displayCitations"
            :key="cit.citation_id"
            @click="$emit('citationClick', cit)"
            class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] text-slate-600 hover:text-blue-600 hover:bg-slate-100 transition-colors font-mono cursor-pointer"
            :title="`查看原文: ${cit.document_title} 第 ${cit.page_idx + 1} 页`"
          >
            <span class="w-1.5 h-1.5 rounded-full bg-blue-500"></span>
            <span class="font-bold text-slate-700">[{{ cit.citation_id }}]</span>
            <span class="truncate max-w-[200px]">{{ cit.document_title }}</span>
            <span class="text-slate-400 text-[10px]">P.{{ (cit.page_idx || 0) + 1 }}</span>
          </button>
        </div>

      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import MarkdownIt from 'markdown-it';
import type { ChatMessage as ChatMessageType, CitationSource } from '../../types';
import ThoughtTimeline from './ThoughtTimeline.vue';

const props = defineProps<{
  message: ChatMessageType;
}>();

const emit = defineEmits<{
  (e: 'citationClick', citation: CitationSource): void;
}>();

const md = new MarkdownIt({
  html: true,
  linkify: true,
  breaks: true,
});

const copied = ref(false);
const contentRef = ref<HTMLElement | null>(null);

// ==================== 1. 深度推理过程折叠状态管理 ====================
const isReasoningExpanded = ref(false);

const hasReasoning = computed(() => {
  return Boolean(
    props.message.isReasoning ||
    (props.message.reasoning_content && props.message.reasoning_content.trim().length > 0)
  );
});

const cleanReasoningContent = computed(() => {
  if (!props.message.reasoning_content) return '';
  return props.message.reasoning_content
    .replace(/<think>/g, '')
    .replace(/<\/think>/g, '')
    .trim();
});

const reasoningDurationText = computed(() => {
  if (!props.message.reasoning_content) return '';
  const charCount = props.message.reasoning_content.trim().length;
  return `已完成推理分析 · 约 ${charCount} 字`;
});

// 监听思考态切换：思考中默认展开推流；正文开始出字（思考完成）自动收起
watch(
  () => props.message.isReasoning,
  (newVal, oldVal) => {
    if (newVal) {
      isReasoningExpanded.value = true;
    } else if (oldVal && !newVal) {
      // 思考结束瞬间平滑收起
      isReasoningExpanded.value = false;
    }
  },
  { immediate: true }
);

// ==================== 2. 正文纯净 Markdown 与角标渲染 ====================
const cleanContentText = computed(() => {
  if (!props.message.content) return '';
  // 彻底剔除任何闭合与未闭合的 <think> 标签残留，严防污染正文 (推流期间不使用 trim，避免丢失自然分词空格)
  return props.message.content
    .replace(/<think>[\s\S]*?<\/think>\s*/g, '')
    .replace(/<think>[\s\S]*$/g, '');
});

const hasCleanContent = computed(() => {
  return cleanContentText.value.trim().length > 0;
});

// 提取正文中实际引用的 [1], [2] 角标 ID 集合
const citedIds = computed(() => {
  if (!hasCleanContent.value) return new Set<number>();
  const matches = cleanContentText.value.matchAll(/\[(\d+)\]/g);
  const ids = new Set<number>();
  for (const m of matches) {
    ids.add(Number(m[1]));
  }
  return ids;
});

// 仅呈现正文中实际采纳、有据可查的参考切片，严禁出现“回答未找到依据却展示5个来源”的自相矛盾
const displayCitations = computed(() => {
  if (!props.message.citations || props.message.citations.length === 0) return [];
  if (citedIds.value.size === 0) return [];
  return props.message.citations.filter((c) => citedIds.value.has(c.citation_id));
});

const renderedMarkdown = computed(() => {
  if (!hasCleanContent.value) return '';
  let rawHtml = md.render(cleanContentText.value);

  // 将 [1], [2], [3] 转换为纯文本高亮超链接 (彻底去除边框和底色小方块)
  rawHtml = rawHtml.replace(/\[(\d+)\]/g, (_match, id) => {
    return `<span class="citation-badge text-xs font-mono font-medium text-blue-600 hover:text-blue-800 hover:underline cursor-pointer align-baseline mx-0.5" data-citation-id="${id}">[${id}]</span>`;
  });

  return rawHtml;
});

const handleContentClick = (e: MouseEvent) => {
  const target = (e.target as HTMLElement).closest('.citation-badge') as HTMLElement | null;
  if (!target) return;

  const citId = Number(target.getAttribute('data-citation-id'));
  if (isNaN(citId)) return;

  const found = props.message.citations?.find((c: CitationSource) => c.citation_id === citId);
  if (found) {
    emit('citationClick', found);
  } else if (props.message.citations && props.message.citations.length > 0) {
    const idx = (citId - 1) % props.message.citations.length;
    emit('citationClick', props.message.citations[idx]);
  }
};

const copyContent = async () => {
  if (!cleanContentText.value) return;
  try {
    await navigator.clipboard.writeText(cleanContentText.value);
    copied.value = true;
    setTimeout(() => {
      copied.value = false;
    }, 2000);
  } catch (err) {
    console.error('Copy failed:', err);
  }
};
</script>
