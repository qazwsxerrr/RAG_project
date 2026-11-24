<template>
  <Teleport to="body">
    <div
      v-if="visible"
      role="dialog"
      aria-modal="true"
      aria-label="Markdown 文档预览"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/45 backdrop-blur-xs p-4 sm:p-6"
      @click.self="$emit('close')"
    >
      <div
        class="bg-white border border-slate-200 rounded-2xl shadow-2xl w-full max-w-4xl h-[86vh] flex flex-col overflow-hidden"
        @click.stop
      >
        <div class="px-5 py-3.5 border-b border-slate-200 bg-slate-50/80 flex items-center justify-between gap-3 shrink-0">
          <h3 class="text-sm font-bold text-slate-900 truncate" :title="fileName">{{ fileName }}</h3>
          <div class="flex items-center gap-2 shrink-0">
            <button
              type="button"
              @click="openDocumentPreviewWithTicket(docId)"
              class="px-2.5 py-1.5 rounded-lg border border-slate-200 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium cursor-pointer"
              title="在新标签页打开原始文本"
            >
              新标签打开
            </button>
            <button
              type="button"
              @click="$emit('close')"
              aria-label="关闭预览"
              title="关闭预览 (Esc)"
              class="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-200/60 cursor-pointer"
            >
              <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>
        </div>

        <div class="flex-1 overflow-y-auto px-6 sm:px-8 py-6 bg-white">
          <div v-if="loading" class="h-full flex items-center justify-center text-xs text-slate-500 gap-2">
            <span class="w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"></span>
            <span>正在加载文档...</span>
          </div>

          <div v-else-if="errorMsg" class="p-4 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700 space-y-2">
            <div class="font-semibold">文档预览加载失败：{{ errorMsg }}</div>
            <button
              type="button"
              @click="loadMarkdown"
              class="px-3 py-1 rounded-lg bg-white border border-rose-200 text-rose-700 hover:bg-rose-50 font-medium cursor-pointer"
            >
              重试
            </button>
          </div>

          <article v-else class="markdown-body select-text" v-html="renderedHtml"></article>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { documentsApi } from '../../api/documents';
import { createMarkdownRenderer } from '../../utils/markdown';
import { openDocumentPreviewWithTicket } from '../../utils/secureMedia';

const props = defineProps<{
  visible: boolean;
  docId: string;
  fileName: string;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
}>();

const md = createMarkdownRenderer({ html: false });
const loading = ref(false);
const errorMsg = ref('');
const rawMarkdown = ref('');
let loadSeq = 0;

const renderedHtml = computed(() => (rawMarkdown.value ? md.render(rawMarkdown.value) : ''));

const loadMarkdown = async () => {
  if (!props.docId) return;
  const seq = ++loadSeq;
  loading.value = true;
  errorMsg.value = '';
  rawMarkdown.value = '';
  try {
    const ticketResp = await documentsApi.createStreamTicket(props.docId, 'preview');
    const url = `/api/v1/documents/${encodeURIComponent(props.docId)}/preview?ticket=${encodeURIComponent(ticketResp.ticket)}`;
    const resp = await fetch(url, { credentials: 'include' });
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const body = await resp.json();
        if (body?.detail) detail = String(body.detail);
      } catch {
        // ignore non-JSON body
      }
      throw new Error(detail);
    }
    const text = await resp.text();
    if (seq === loadSeq) rawMarkdown.value = text;
  } catch (err: any) {
    if (seq === loadSeq) errorMsg.value = err?.message || '无法加载文档内容';
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
};

const handleKeydown = (e: KeyboardEvent) => {
  if (e.key === 'Escape' && props.visible) emit('close');
};

watch(
  () => [props.visible, props.docId] as const,
  ([vis, id]) => {
    if (vis) {
      window.addEventListener('keydown', handleKeydown);
      if (id) void loadMarkdown();
    } else {
      window.removeEventListener('keydown', handleKeydown);
      rawMarkdown.value = '';
      errorMsg.value = '';
    }
  },
  { immediate: true }
);

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown);
});
</script>
