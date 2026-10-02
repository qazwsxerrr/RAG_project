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

        <!-- 错误提示卡片 -->
        <div v-if="message.error" class="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700 flex items-start gap-2">
          <svg class="w-4 h-4 text-rose-500 mt-0.5 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          <div class="space-y-0.5">
            <div class="font-semibold text-rose-800">推理检索发生异常</div>
            <div class="font-mono text-[11px] text-rose-600">{{ message.error }}</div>
          </div>
        </div>

        <!-- 正文 Markdown 渲染区域 (仅在实际有文本内容时渲染，避免未出字前孤立出现蓝色方块) -->
        <div v-if="message.content" class="relative group pt-1">
          <!-- 正文渲染与角标点击捕获 -->
          <div 
            ref="contentRef"
            @click="handleContentClick"
            class="markdown-body select-text text-slate-800 text-xs sm:text-sm leading-relaxed inline"
            v-html="renderedMarkdown"
          ></div>

          <!-- 流式打字机闪烁光标 (极细自然文字光标，随正文自然流动，消除突兀蓝色实心方块) -->
          <span v-if="message.isStreaming" class="inline-block w-[2px] h-[1.1em] ml-0.5 bg-slate-700 animate-pulse align-middle"></span>

          <!-- 底部轻量辅助条：字数统计与复制回答 -->
          <div v-if="!message.isStreaming && message.content" class="mt-2.5 pt-2 flex items-center justify-between text-[11px] text-slate-400 select-none">
            <span class="font-mono text-[10px] text-slate-400">
              {{ message.content.length }} 字 · {{ citedIds.size > 0 ? `已标注 ${citedIds.size} 处溯源依据` : '知识库无直接关联依据 (防幻觉保护)' }}
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
        <div v-if="message.isStreaming && !message.content" class="text-xs text-slate-500 flex items-center gap-2 py-1 select-none">
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
import { ref, computed } from 'vue';
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

// 提取正文中实际引用的 [1], [2] 角标 ID 集合
const citedIds = computed(() => {
  if (!props.message.content) return new Set<number>();
  const cleanContent = props.message.content.replace(/<think>[\s\S]*?<\/think>\s*/g, '');
  const matches = cleanContent.matchAll(/\[(\d+)\]/g);
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
  if (!props.message.content) return '';
  // 防御性过滤可能意外残留的 <think> 思考标签
  const cleanContent = props.message.content.replace(/<think>[\s\S]*?<\/think>\s*/g, '');
  let rawHtml = md.render(cleanContent);

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
  if (!props.message.content) return;
  try {
    await navigator.clipboard.writeText(props.message.content);
    copied.value = true;
    setTimeout(() => {
      copied.value = false;
    }, 2000);
  } catch (err) {
    console.error('Copy failed:', err);
  }
};
</script>
