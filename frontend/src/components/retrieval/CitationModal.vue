<template>
  <Teleport to="body">
    <div 
      v-if="visible && citation" 
      class="fixed inset-y-0 right-0 left-0 md:left-60 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-xs p-4 sm:p-6 animate-in fade-in duration-200"
      @click.self="$emit('close')"
    >
      <div 
        class="bg-white border border-slate-200/90 rounded-2xl shadow-2xl max-w-3xl w-full p-5 sm:p-6 text-slate-800 flex flex-col max-h-[92vh] relative z-10"
        @click.stop
      >
      
      <!-- 弹窗顶栏 -->
      <div class="flex items-center justify-between pb-3.5 border-b border-slate-100">
        <div>
          <div class="flex items-center space-x-2">
            <span class="text-xs font-mono font-semibold text-slate-800 bg-slate-100 px-2 py-0.5 rounded">
              [{{ citation.citation_id }}]
            </span>
            <h4 class="text-sm font-semibold text-slate-900 truncate max-w-lg">
              {{ citation.document_title }}
            </h4>
          </div>
          <p class="text-xs text-slate-500 mt-1 font-medium flex items-center gap-1.5 flex-wrap">
            <span class="text-slate-700">{{ citation.chunk_label || '切片来源' }}</span>
            <span class="text-slate-300">·</span>
            <span class="text-slate-600">原文档第 {{ displayPage }} 页</span>
            <span v-if="citation.score" class="text-slate-400">
              · (相似度: {{ (citation.score * 100).toFixed(1) }}%)
            </span>
          </p>
        </div>

        <button 
          @click="$emit('close')" 
          class="text-slate-400 hover:text-slate-700 transition-colors text-2xl p-1 leading-none"
        >
          &times;
        </button>
      </div>

      <!-- 阅览视图模式切换 (切片文字 vs 原版高精 PDF 对照) -->
      <div class="pt-3 pb-2 flex items-center justify-between select-none">
        <div class="inline-flex p-0.5 bg-slate-100 rounded-lg text-xs">
          <button 
            @click="activeTab = 'snippet'"
            class="px-3 py-1 rounded-md transition-all font-medium"
            :class="activeTab === 'snippet' ? 'bg-white text-slate-900 shadow-xs font-semibold' : 'text-slate-500 hover:text-slate-800'"
          >
            切片摘要与原图
          </button>
          <button 
            @click="activeTab = 'pdf'"
            class="px-3 py-1 rounded-md transition-all font-medium flex items-center gap-1.5"
            :class="activeTab === 'pdf' ? 'bg-white text-slate-900 shadow-xs font-semibold' : 'text-slate-500 hover:text-slate-800'"
          >
            <span>原生 PDF 对照 (P.{{ targetPage }})</span>
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
          </button>
        </div>

        <!-- 视图 2 的工具栏 (仅在 activeTab === 'pdf' 时展示) -->
        <div v-show="activeTab === 'pdf'" class="flex items-center space-x-2">
          <!-- 翻页器 -->
          <div class="flex items-center bg-slate-50 border border-slate-200 rounded-lg px-2 py-0.5 space-x-1.5 text-xs">
            <button 
              @click="prevPage" 
              :disabled="targetPage <= 1"
              class="text-slate-500 hover:text-slate-900 disabled:opacity-30 disabled:cursor-not-allowed px-1"
              title="上一页"
            >
              ◀
            </button>
            <span class="text-slate-700 font-mono font-medium">
              第 {{ targetPage }} / {{ totalPages }} 页
            </span>
            <button 
              @click="nextPage" 
              :disabled="targetPage >= totalPages"
              class="text-slate-500 hover:text-slate-900 disabled:opacity-30 disabled:cursor-not-allowed px-1"
              title="下一页"
            >
              ▶
            </button>
            <button 
              v-if="targetPage !== displayPage" 
              @click="targetPage = displayPage"
              class="text-[11px] text-indigo-600 hover:underline pl-1 border-l border-slate-200"
            >
              回引用页
            </button>
          </div>

          <!-- 缩放控制 -->
          <div class="flex items-center bg-slate-50 border border-slate-200 rounded-lg px-2 py-0.5 space-x-1 text-xs">
            <button @click="zoomOut" class="px-1 text-slate-600 hover:text-slate-900 font-bold" title="缩小">-</button>
            <span class="text-slate-700 font-mono w-10 text-center">{{ Math.round(zoomScale * 100) }}%</span>
            <button @click="zoomIn" class="px-1 text-slate-600 hover:text-slate-900 font-bold" title="放大">+</button>
            <button @click="resetZoom" class="text-[11px] text-slate-500 hover:text-slate-800 pl-1 border-l border-slate-200">适应</button>
          </div>
        </div>
      </div>

      <!-- 视图 1：原文切片片段、富媒体原图与原生 HTML 表格呈现 -->
      <div v-show="activeTab === 'snippet'" class="py-2 space-y-3 flex-1 overflow-y-auto">
        <!-- 资产原图 (MinerU 高精截取的真实表格/图表原图) -->
        <div v-if="citation.asset_proxy_url" class="bg-slate-50 border border-slate-200/80 rounded-xl p-3.5 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
              <span>🖼️</span> 原版版面提取截取图 (MinerU 原生抽取)
            </span>
            <a 
              :href="citation.asset_proxy_url" 
              target="_blank" 
              class="text-xs text-indigo-600 hover:text-indigo-800 font-medium"
            >
              在新标签页查看高清原图 ↗
            </a>
          </div>
          <div class="flex justify-center bg-white rounded-lg p-2.5 border border-slate-200 shadow-inner max-h-72 overflow-y-auto">
            <img 
              :src="citation.asset_proxy_url" 
              alt="版面截取原图" 
              class="max-h-64 max-w-full object-contain rounded"
            />
          </div>
        </div>

        <!-- 表格或代码富文本呈现 (对应实拍 Img 14) -->
        <div v-if="citation.table_html" class="space-y-1.5">
          <label class="text-xs font-semibold text-slate-600">
            {{ citation.is_table ? '📊 结构化表格内容：' : '💻 原始代码与流程图：' }}
          </label>
          <div 
            class="border border-slate-200 rounded-xl overflow-x-auto p-4 bg-white text-xs markdown-body shadow-xs max-h-60"
            v-html="citation.table_html"
          ></div>
        </div>

        <!-- 文本切片摘要 (展示 AI 总结或黄金文本段落) -->
        <div class="space-y-1.5">
          <label class="text-xs font-semibold text-slate-600">
            {{ citation.table_html ? '💡 AI 语义摘要 / 业务说明：' : '📄 检索提取的黄金文本段落：' }}
          </label>
          <div 
            class="text-xs font-mono text-slate-800 leading-relaxed bg-slate-50 p-4 rounded-xl border border-slate-200 max-h-80 overflow-y-auto whitespace-pre-wrap select-text"
          >
            {{ citation.snippet }}
          </div>
        </div>
      </div>

      <!-- 视图 2：原生高精 PDF 单页渲染对照 (精准定位第 X 页，杜绝 iframe 乱跳封面与裁切失真) -->
      <div v-show="activeTab === 'pdf'" class="py-2 flex-1 flex flex-col min-h-[460px] overflow-hidden">
        <div class="flex-1 rounded-xl overflow-auto border border-slate-200 shadow-inner bg-slate-100 relative">
          <div class="min-h-full flex items-start justify-center p-4">
            <!-- 加载中遮罩 -->
            <div 
              v-if="loadingPage" 
              class="absolute inset-0 bg-white/80 backdrop-blur-xs flex items-center justify-center z-10 text-xs text-indigo-600 font-medium gap-2"
            >
              <span class="inline-block w-4 h-4 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin"></span>
              <span>正在高精保真渲染 PDF 第 {{ targetPage }} 页...</span>
            </div>

            <!-- 单页保真图片 (服务端直接渲染生成，永远不会跳错页码，从页顶自然垂直滚动，绝不发生裁切) -->
            <img 
              :src="currentRenderUrl" 
              @load="loadingPage = false"
              @error="loadingPage = false"
              :style="{ width: `${zoomScale * 100}%`, maxWidth: 'none' }"
              class="transition-all duration-150 shadow-md rounded border border-slate-300 bg-white block select-none shrink-0"
              alt="PDF 原文页面预览"
            />
          </div>
        </div>
      </div>

      <!-- 底部操作：坐标区域说明与原文档独立外链 (对应实拍 Img 13、19、28) -->
      <div class="pt-3.5 border-t border-slate-100 flex items-center justify-between">
        <span class="text-[11px] text-slate-400 font-mono">
          原档共 {{ totalPages }} 页 · 原生高清版面呈现
        </span>

        <div class="flex items-center space-x-2.5">
          <a 
            :href="pdfExternalUrl" 
            target="_blank" 
            class="text-xs text-blue-600 hover:text-blue-700 font-medium flex items-center gap-1 transition-colors"
          >
            <span>在新标签页打开 PDF (第 {{ displayPage }} 页)</span>
            <span>&rarr;</span>
          </a>

          <button 
            @click="$emit('close')" 
            class="px-3.5 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-medium rounded-lg transition-colors"
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
import { ref, computed, watch } from 'vue';
import type { CitationSource } from '../../types';

const props = defineProps<{
  visible: boolean;
  citation: CitationSource | null;
}>();

defineEmits<{
  (e: 'close'): void;
}>();

const activeTab = ref<'snippet' | 'pdf'>('snippet');
const targetPage = ref<number>(1);
const totalPages = ref<number>(41);
const zoomScale = ref<number>(0.85);
const loadingPage = ref<boolean>(false);

const displayPage = computed(() => {
  if (!props.citation) return 1;
  if (props.citation.display_page) return props.citation.display_page;
  return (props.citation.page_idx ?? 0) + 1;
});

watch(
  () => props.citation,
  (newCit) => {
    if (newCit) {
      targetPage.value = displayPage.value;
      totalPages.value = newCit.total_pages || 41;
      zoomScale.value = 0.85;
      activeTab.value = 'snippet';
    }
  },
  { immediate: true }
);

// 服务端渲染的单页 PNG 图片地址 (纯净原生 PDF 版面，彻底移除冗余杂乱色块高亮框)
const currentRenderUrl = computed(() => {
  if (!props.citation) return '';
  const base = `/api/v1/documents/${props.citation.document_id}/pages/${targetPage.value}`;
  const params = new URLSearchParams();
  params.set('scale', '2.0');
  loadingPage.value = true;
  return `${base}?${params.toString()}`;
});

// 新标签页打开原生 PDF 的 URL
const pdfExternalUrl = computed(() => {
  if (!props.citation) return '#';
  const base = props.citation.preview_url || `/api/v1/documents/${props.citation.document_id}/preview`;
  return `${base}#page=${displayPage.value}`;
});

const prevPage = () => {
  if (targetPage.value > 1) {
    targetPage.value--;
  }
};

const nextPage = () => {
  if (targetPage.value < totalPages.value) {
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
</script>
