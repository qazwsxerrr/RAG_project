<template>
  <!-- 形态 1 (inline): 问答消息底部的行内紧凑溯源胶囊按钮 -->
  <button
    v-if="variant === 'inline'"
    type="button"
    @click="$emit('select', citation)"
    class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] text-slate-600 hover:text-blue-600 hover:bg-slate-100 transition-colors font-mono cursor-pointer"
    :title="`查看原文: ${citation.document_title} 第 ${displayPage} 页`"
  >
    <span class="w-1.5 h-1.5 rounded-full bg-blue-500"></span>
    <span class="font-bold text-slate-700">[{{ citation.citation_id }}]</span>
    <span class="truncate max-w-[200px]">{{ citation.document_title }}</span>
    <span class="text-slate-400 text-[10px]">P.{{ displayPage }}</span>
  </button>

  <!-- 形态 2 (header): 溯源详情弹窗顶部的结构化标题与元信息栏 -->
  <div v-else class="min-w-0">
    <div class="flex items-center space-x-2">
      <span class="text-xs font-mono font-semibold text-slate-800 bg-slate-100 px-2 py-0.5 rounded shrink-0">
        [{{ citation.citation_id }}]
      </span>
      <h4 class="text-sm font-semibold text-slate-900 truncate max-w-lg" :title="citation.document_title">
        {{ citation.document_title }}
      </h4>
    </div>
    <p class="text-xs text-slate-500 mt-1 font-medium flex items-center gap-1.5 flex-wrap">
      <span class="text-slate-700">{{ citation.chunk_label || '引用片段' }}</span>
      <template v-if="isPdfDoc">
        <span class="text-slate-300">·</span>
        <span class="text-slate-600">第 {{ displayPage }} 页</span>
      </template>
      <span v-if="showScore && citation.score" class="text-slate-400">
        · 相关度 {{ (citation.score * 100).toFixed(1) }}%
      </span>
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { CitationSource } from '../../types';

const props = withDefaults(
  defineProps<{
    citation: CitationSource;
    variant?: 'inline' | 'header';
    showScore?: boolean;
  }>(),
  {
    variant: 'inline',
    showScore: true,
  }
);

defineEmits<{
  (e: 'select', citation: CitationSource): void;
}>();

const displayPage = computed(() => {
  if (props.citation.display_page) return props.citation.display_page;
  return (props.citation.page_idx ?? 0) + 1;
});

const isPdfDoc = computed(() => {
  if (typeof props.citation.is_pdf === 'boolean') {
    return props.citation.is_pdf || Boolean(props.citation.document_title?.toLowerCase().endsWith('.pdf'));
  }
  return Boolean(props.citation.document_title?.toLowerCase().endsWith('.pdf'));
});
</script>
