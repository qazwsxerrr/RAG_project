<template>
  <Teleport to="body">
    <div 
      v-if="visible && citation" 
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-sm p-4 sm:p-6 animate-in fade-in duration-200"
      @click.self="$emit('close')"
    >
      <div 
        class="bg-white border border-slate-200/90 rounded-2xl shadow-2xl max-w-3xl w-full p-5 sm:p-6 text-slate-800 flex flex-col max-h-[92vh] relative z-10"
        @click.stop
      >
      
      <!-- 弹窗顶栏 -->
      <div class="flex items-center justify-between pb-3.5 border-b border-slate-100">
        <div class="min-w-0">
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
            <span v-if="citation.score" class="text-slate-400">
              · 相关度 {{ (citation.score * 100).toFixed(1) }}%
            </span>
          </p>
        </div>

        <button 
          @click="$emit('close')" 
          class="text-slate-400 hover:text-slate-700 transition-colors text-2xl p-1 leading-none cursor-pointer"
        >
          &times;
        </button>
      </div>

      <!-- 阅览视图模式切换：仅 PDF 文档展示原文单页预览 Tab 与翻页器 -->
      <div v-if="isPdfDoc" class="pt-3 pb-2 flex flex-wrap items-center justify-between gap-2 select-none">
        <div class="inline-flex p-0.5 bg-slate-100 rounded-lg text-xs">
          <button 
            @click="activeTab = 'snippet'"
            class="px-3 py-1 rounded-md transition-all font-medium cursor-pointer"
            :class="activeTab === 'snippet' ? 'bg-white text-slate-900 shadow-sm font-semibold' : 'text-slate-500 hover:text-slate-800'"
          >
            引用内容与图表
          </button>
          <button 
            @click="switchToPdfTab"
            class="px-3 py-1 rounded-md transition-all font-medium flex items-center gap-1.5 cursor-pointer"
            :class="activeTab === 'pdf' ? 'bg-white text-slate-900 shadow-sm font-semibold' : 'text-slate-500 hover:text-slate-800'"
          >
            <span>原文页预览 (第 {{ targetPage }} 页)</span>
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
          </button>
        </div>

        <!-- 视图 2 的工具栏 (仅在 activeTab === 'pdf' 时展示) -->
        <div v-show="activeTab === 'pdf'" class="flex items-center space-x-2">
          <!-- 翻页器 -->
          <div class="flex items-center bg-slate-50 border border-slate-200 rounded-lg px-2 py-0.5 space-x-1.5 text-xs">
            <button 
              @click="prevPage" 
              :disabled="targetPage <= 1 || loadingPage"
              class="text-slate-500 hover:text-slate-900 disabled:opacity-30 disabled:cursor-not-allowed px-1 cursor-pointer"
              title="上一页"
            >
              ◀
            </button>
            <span class="text-slate-700 font-mono font-medium">
              第 {{ targetPage }}<template v-if="totalPages !== null"> / {{ totalPages }}</template> 页
            </span>
            <button 
              @click="nextPage" 
              :disabled="(totalPages !== null && targetPage >= totalPages) || loadingPage"
              class="text-slate-500 hover:text-slate-900 disabled:opacity-30 disabled:cursor-not-allowed px-1 cursor-pointer"
              title="下一页"
            >
              ▶
            </button>
            <button 
              v-if="targetPage !== displayPage" 
              @click="targetPage = displayPage"
              class="text-[11px] text-indigo-600 hover:underline pl-1 border-l border-slate-200 cursor-pointer"
            >
              回引用页
            </button>
          </div>

          <!-- 缩放控制 -->
          <div class="flex items-center bg-slate-50 border border-slate-200 rounded-lg px-2 py-0.5 space-x-1 text-xs">
            <button @click="zoomOut" class="px-1 text-slate-600 hover:text-slate-900 font-bold cursor-pointer" title="缩小">-</button>
            <span class="text-slate-700 font-mono w-10 text-center">{{ Math.round(zoomScale * 100) }}%</span>
            <button @click="zoomIn" class="px-1 text-slate-600 hover:text-slate-900 font-bold cursor-pointer" title="放大">+</button>
            <button @click="resetZoom" class="text-[11px] text-slate-500 hover:text-slate-800 pl-1 border-l border-slate-200 cursor-pointer">适应</button>
          </div>
        </div>
      </div>

      <!-- 视图 1：原文切片片段、富媒体原图与原生 HTML 表格呈现 -->
      <div v-show="activeTab === 'snippet' || !isPdfDoc" class="py-3 space-y-3 flex-1 overflow-y-auto">
        <div v-if="citation.asset_proxy_url" class="bg-slate-50 border border-slate-200/80 rounded-xl p-3.5 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-semibold text-slate-700">原文图表截图</span>
            <a 
              :href="withTokenUrl(citation.asset_proxy_url)" 
              target="_blank" 
              class="text-xs text-indigo-600 hover:text-indigo-800 font-medium"
            >
              在新标签页查看原图 ↗
            </a>
          </div>
          <div class="flex justify-center bg-white rounded-lg p-2.5 border border-slate-200 shadow-inner max-h-72 overflow-y-auto">
            <img 
              :src="withTokenUrl(citation.asset_proxy_url)" 
              alt="原文图表截图" 
              class="max-h-64 max-w-full object-contain rounded"
            />
          </div>
        </div>

        <div v-if="citation.table_html" class="space-y-1.5">
          <label class="text-xs font-semibold text-slate-600">
            {{ citation.is_table ? '表格内容：' : '代码与流程图：' }}
          </label>
          <div 
            class="border border-slate-200 rounded-xl overflow-x-auto p-4 bg-white text-xs markdown-body shadow-sm max-h-60"
            v-html="citation.table_html"
          ></div>
        </div>

        <div class="space-y-1.5">
          <label class="text-xs font-semibold text-slate-600">
            {{ citation.table_html ? '内容说明：' : '原文片段：' }}
          </label>
          <div 
            class="text-xs font-mono text-slate-800 leading-relaxed bg-slate-50 p-4 rounded-xl border border-slate-200 max-h-80 overflow-y-auto whitespace-pre-wrap select-text"
          >
            {{ citation.snippet }}
          </div>
        </div>
      </div>

      <!-- 视图 2：PDF 单页图片渲染对照 -->
      <div v-if="isPdfDoc" v-show="activeTab === 'pdf'" class="py-2 flex-1 flex flex-col min-h-[420px] overflow-hidden">
        <div class="flex-1 rounded-xl overflow-auto border border-slate-200 shadow-inner bg-slate-100 relative">
          <div class="min-h-full flex items-start justify-center p-4">
            <!-- 加载中遮罩 -->
            <div 
              v-if="loadingPage" 
              class="absolute inset-0 bg-white/80 backdrop-blur-sm flex items-center justify-center z-10 text-xs text-indigo-600 font-medium gap-2"
            >
              <span class="inline-block w-4 h-4 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin"></span>
              <span>正在加载第 {{ targetPage }} 页...</span>
            </div>

            <div
              v-else-if="pageRenderError"
              class="my-auto p-6 bg-white rounded-xl border border-rose-200 text-center space-y-3 max-w-sm"
            >
              <div class="text-xs font-semibold text-rose-700">页面预览加载失败</div>
              <div class="text-[11px] text-slate-500 break-all">{{ pageRenderError }}</div>
              <button
                @click="loadPdfPageBlob"
                class="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-xs font-medium rounded-lg transition-colors cursor-pointer"
              >
                重试加载
              </button>
            </div>

            <img 
              v-else-if="pageBlobUrl"
              :src="pageBlobUrl" 
              :style="{ width: `${zoomScale * 100}%`, maxWidth: 'none' }"
              class="transition-all duration-150 shadow-md rounded border border-slate-300 bg-white block select-none shrink-0"
              alt="PDF 原文页面预览"
            />
          </div>
        </div>
      </div>

      <!-- 底部操作 -->
      <div class="pt-3.5 border-t border-slate-100 flex items-center justify-between">
        <span class="text-[11px] text-slate-400 font-mono">
          <template v-if="isPdfDoc && totalPages !== null">共 {{ totalPages }} 页</template>
          <template v-else-if="isPdfDoc">PDF 文档 · 第 {{ displayPage }} 页</template>
          <template v-else>{{ fileFormatLabel }}</template>
        </span>

        <div class="flex items-center space-x-2.5">
          <a 
            :href="externalPreviewUrl" 
            target="_blank" 
            class="text-xs text-blue-600 hover:text-blue-700 font-medium flex items-center gap-1 transition-colors"
          >
            <span>{{ isPdfDoc ? `在新标签页打开 PDF (第 ${displayPage} 页)` : '查看原文档' }}</span>
            <span>&rarr;</span>
          </a>

          <button 
            @click="$emit('close')" 
            class="px-3.5 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-medium rounded-lg transition-colors cursor-pointer"
          >
            关闭
          </button>
        </div>
      </div>

    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from 'vue';
import type { CitationSource } from '../../types';
import { useAuthStore, isTokenExpired, withTokenUrl } from '../../stores/auth';

const props = defineProps<{
  visible: boolean;
  citation: CitationSource | null;
}>();

defineEmits<{
  (e: 'close'): void;
}>();

const authStore = useAuthStore();
const activeTab = ref<'snippet' | 'pdf'>('snippet');
const targetPage = ref<number>(1);
const totalPages = ref<number | null>(null);
const zoomScale = ref<number>(0.85);
const loadingPage = ref<boolean>(false);
const pageRenderError = ref<string>('');
const pageBlobUrl = ref<string>('');
let pageAbortController: AbortController | null = null;

const displayPage = computed(() => {
  if (!props.citation) return 1;
  if (props.citation.display_page) return props.citation.display_page;
  return (props.citation.page_idx ?? 0) + 1;
});

const isPdfDoc = computed(() => {
  if (!props.citation) return false;
  if (typeof props.citation.is_pdf === 'boolean') {
    return props.citation.is_pdf || Boolean(props.citation.document_title?.toLowerCase().endsWith('.pdf'));
  }
  return Boolean(props.citation.document_title?.toLowerCase().endsWith('.pdf'));
});

const fileFormatLabel = computed(() => {
  const title = props.citation?.document_title || '';
  const ext = title.includes('.') ? title.split('.').pop()?.toUpperCase() : '';
  return ext ? `${ext} 文档` : '文本文档';
});

const abortInflightPageLoad = () => {
  if (pageAbortController) {
    pageAbortController.abort();
    pageAbortController = null;
  }
};

const revokeBlobUrl = () => {
  if (pageBlobUrl.value) {
    URL.revokeObjectURL(pageBlobUrl.value);
    pageBlobUrl.value = '';
  }
};

const loadPdfPageBlob = async () => {
  if (!props.visible || !props.citation || !isPdfDoc.value || activeTab.value !== 'pdf') return;
  abortInflightPageLoad();
  const controller = new AbortController();
  pageAbortController = controller;
  loadingPage.value = true;
  pageRenderError.value = '';
  const docId = props.citation.document_id;
  const pageNum = targetPage.value;
  const url = `/api/v1/documents/${docId}/pages/${pageNum}?scale=2.0`;

  try {
    let currentToken = authStore.token;
    if (!currentToken || isTokenExpired(currentToken)) {
      currentToken = await authStore.refreshToken();
    }

    const doFetch = (tk: string) => {
      const headers: Record<string, string> = {};
      if (tk) {
        headers['Authorization'] = `Bearer ${tk}`;
      }
      return fetch(url, { headers, signal: controller.signal });
    };

    let resp = await doFetch(currentToken);
    if (resp.status === 401) {
      const refreshedToken = await authStore.refreshToken();
      resp = await doFetch(refreshedToken);
    }

    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const errJson = await resp.json();
        detail = errJson.detail || detail;
      } catch {
        // ignore
      }
      throw new Error(detail);
    }
    const headerTotal = resp.headers.get('X-Total-Pages');
    if (headerTotal) {
      const parsed = parseInt(headerTotal, 10);
      if (!Number.isNaN(parsed) && parsed > 0) {
        totalPages.value = parsed;
      }
    }
    const blob = await resp.blob();
    if (controller.signal.aborted || pageAbortController !== controller) return;
    revokeBlobUrl();
    pageBlobUrl.value = URL.createObjectURL(blob);
  } catch (err: any) {
    if (err?.name === 'AbortError' || controller.signal.aborted) {
      return;
    }
    revokeBlobUrl();
    pageRenderError.value = err?.message || '无法加载该页预览图片';
  } finally {
    if (pageAbortController === controller) {
      loadingPage.value = false;
    }
  }
};

const switchToPdfTab = () => {
  activeTab.value = 'pdf';
  loadPdfPageBlob();
};

watch(
  () => [props.visible, props.citation] as const,
  ([vis, newCit]) => {
    abortInflightPageLoad();
    revokeBlobUrl();
    pageRenderError.value = '';
    if (newCit) {
      targetPage.value = displayPage.value;
      totalPages.value = typeof newCit.total_pages === 'number' && newCit.total_pages > 0 ? newCit.total_pages : null;
      zoomScale.value = 0.85;
      activeTab.value = 'snippet';
    }
    if (!vis) {
      loadingPage.value = false;
    }
  },
  { immediate: true }
);

watch(targetPage, () => {
  if (props.visible && isPdfDoc.value && activeTab.value === 'pdf') {
    loadPdfPageBlob();
  }
});

const externalPreviewUrl = computed(() => {
  if (!props.citation) return '#';
  const base = props.citation.preview_url || `/api/v1/documents/${props.citation.document_id}/preview`;
  return isPdfDoc.value ? withTokenUrl(`${base}#page=${displayPage.value}`) : withTokenUrl(base);
});

const prevPage = () => {
  if (targetPage.value > 1) {
    targetPage.value--;
  }
};

const nextPage = () => {
  if (totalPages.value === null || targetPage.value < totalPages.value) {
    targetPage.value++;
  }
};

const zoomIn = () => {
  if (zoomScale.value < 1.8) {
    zoomScale.value = Number((zoomScale.value + 0.15).toFixed(2));
  }
};

const zoomOut = () => {
  if (zoomScale.value > 0.4) {
    zoomScale.value = Number((zoomScale.value - 0.15).toFixed(2));
  }
};

const resetZoom = () => {
  zoomScale.value = 0.85;
};

onUnmounted(() => {
  abortInflightPageLoad();
  revokeBlobUrl();
});
</script>
