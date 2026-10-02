<template>
  <Teleport to="body">
    <div v-if="visible" class="fixed inset-y-0 right-0 left-0 md:left-60 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-xs p-4 sm:p-6 animate-in fade-in duration-200" @click.self="$emit('close')">
      <div class="bg-white border border-slate-200 rounded-2xl shadow-xl max-w-4xl w-full p-6 text-slate-800 flex flex-col max-h-[90vh]" @click.stop>
      
      <!-- 弹窗顶栏 -->
      <div class="flex items-center justify-between pb-4 border-b border-slate-100">
        <div>
          <div class="flex items-center gap-2">
            <h3 class="text-base font-bold text-slate-900 flex items-center gap-2">
              <svg class="w-4 h-4 text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
              文档切片资产详情
            </h3>
            <span class="text-xs text-slate-400 font-mono">
              共 {{ chunks.length }} 个切片
            </span>
          </div>
          <p class="text-xs text-slate-500 mt-1 font-mono">
            文档: <span class="text-slate-700 font-medium">{{ fileName }}</span>
          </p>
        </div>
        <button @click="$emit('close')" class="text-slate-400 hover:text-slate-700 text-xl transition-colors">&times;</button>
      </div>

      <!-- 切片列表区域 -->
      <div class="overflow-y-auto py-4 space-y-3.5 flex-1 pr-1">
        <div v-if="loading" class="text-center py-12 text-xs text-slate-500 flex flex-col items-center gap-2">
          <svg class="w-5 h-5 animate-spin text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
          <span>正在检索切片资产与版面元数据...</span>
        </div>

        <div v-else-if="chunks.length === 0" class="text-center py-12 text-xs text-slate-400">
          暂未找到切片数据或文档尚在解析流水线中
        </div>

        <div 
          v-else 
          v-for="chunk in chunks" 
          :key="chunk.chunk_index" 
          class="border border-slate-200 rounded-xl p-4 bg-white hover:border-slate-300 transition-all space-y-2.5"
        >
          <!-- 切片标头 -->
          <div class="flex items-center justify-between text-xs pb-2 border-b border-slate-100">
            <div class="flex items-center space-x-2">
              <span class="font-bold text-slate-800 font-mono">{{ chunk.chunk_label || `第 ${chunk.chunk_index} 片` }}</span>
              <span class="text-slate-300">·</span>
              <span class="text-slate-600">原文档第 {{ chunk.display_page || (chunk.page_idx + 1) }} 页</span>
              <span v-if="chunk.is_table" class="text-amber-600 font-medium text-[11px] flex items-center gap-1">
                <span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>表格
              </span>
              <span v-if="chunk.is_code" class="text-sky-600 font-medium text-[11px] flex items-center gap-1">
                <span class="w-1.5 h-1.5 rounded-full bg-sky-500"></span>代码
              </span>
            </div>

            <div class="text-[11px] text-slate-400 font-mono">
              字数: {{ chunk.content.length }} · 稀疏Token: {{ chunk.sparse_token_count || 0 }}
            </div>
          </div>

          <!-- 面包屑层级导航 -->
          <div v-if="chunk.breadcrumb && chunk.breadcrumb.length > 0" class="text-[11px] text-slate-500 flex items-center gap-1">
            <span class="text-slate-400">章节层级:</span>
            <span v-for="(b, idx) in chunk.breadcrumb" :key="idx" class="flex items-center gap-1">
              <span class="text-slate-600 font-medium">{{ b }}</span>
              <span v-if="idx < chunk.breadcrumb.length - 1" class="text-slate-300">/</span>
            </span>
          </div>

          <!-- 结构化表格 / 原始代码 / 流程图富文本呈现 (支持展示源码与图表) -->
          <div v-if="chunk.table_html" class="p-3 bg-slate-50 border border-slate-200 rounded-lg overflow-x-auto text-xs max-h-56 markdown-body" v-html="chunk.table_html"></div>

          <!-- 切片检索正文 (AI 语义总结) -->
          <div class="text-xs font-mono text-slate-800 leading-relaxed bg-white p-3.5 rounded-lg border border-slate-200 max-h-56 overflow-y-auto whitespace-pre-wrap select-text">
            {{ chunk.content }}
          </div>

          <!-- 关联 OSS 产物链接 -->
          <div v-if="chunk.asset_url" class="flex items-center justify-end text-xs">
            <a 
              :href="chunk.asset_url" 
              target="_blank" 
              class="text-blue-600 hover:text-blue-700 font-medium flex items-center gap-1 transition-colors"
            >
              <span>查看关联多模态资产 (OSS)</span>
              <span>&rarr;</span>
            </a>
          </div>
        </div>
      </div>

      <!-- 底部关闭按钮 -->
      <div class="pt-3 border-t border-slate-100 flex justify-end">
        <button 
          @click="$emit('close')" 
          class="px-5 py-2 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-medium rounded-xl transition-colors"
        >
          关闭
        </button>
      </div>

    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import type { ChunkItem } from '../../types';
import { documentsApi } from '../../api/documents';

const props = defineProps<{
  visible: boolean;
  docId: string;
  fileName: string;
}>();

defineEmits<{
  (e: 'close'): void;
}>();

const chunks = ref<ChunkItem[]>([]);
const loading = ref(false);

watch(
  () => props.docId,
  async (newId) => {
    if (newId && props.visible) {
      loading.value = true;
      try {
        chunks.value = await documentsApi.getDocumentChunks(newId);
      } catch (err) {
        console.warn('加载切片失败:', err);
        chunks.value = [];
      } finally {
        loading.value = false;
      }
    }
  },
  { immediate: true }
);
</script>
