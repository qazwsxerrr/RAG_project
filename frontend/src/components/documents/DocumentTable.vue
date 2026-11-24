<template>
  <div class="glass-panel rounded-2xl overflow-hidden border border-slate-200/90 bg-white shadow-sm">
    <div class="overflow-x-auto">
      <table class="w-full text-left text-xs text-slate-700">
        <thead class="bg-slate-50/90 text-slate-500 font-semibold border-b border-slate-200 uppercase tracking-wider text-[11px] whitespace-nowrap">
          <tr>
            <th class="py-3.5 px-4">文档名称</th>
            <th class="py-3.5 px-4">所属知识库</th>
            <th class="py-3.5 px-4">密级</th>
            <th class="py-3.5 px-4">分类</th>
            <th class="py-3.5 px-4">状态</th>
            <th class="py-3.5 px-4">入库时间</th>
            <th class="py-3.5 px-4 text-right">操作</th>
          </tr>
        </thead>

        <tbody class="divide-y divide-slate-100 font-normal">
          <tr v-if="loading && documents.length === 0">
            <td colspan="7" class="py-12 text-center text-slate-400">
              <span class="inline-flex items-center gap-2">
                <span class="w-3.5 h-3.5 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"></span>
                <span>正在加载文档列表...</span>
              </span>
            </td>
          </tr>
          <tr v-else-if="documents.length === 0">
            <td colspan="7" class="py-12 text-center text-slate-400">
              暂无符合条件的文档
            </td>
          </tr>

          <tr
            v-for="doc in documents"
            :key="doc.doc_id"
            class="hover:bg-slate-50/80 transition-colors group"
          >
            <!-- 文档名称 -->
            <td class="py-3.5 px-4 whitespace-nowrap">
              <div class="flex items-center space-x-2.5 min-w-0 max-w-xs md:max-w-sm">
                <svg v-if="doc.file_name.endsWith('.pdf')" class="w-4 h-4 text-rose-500 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                <svg v-else-if="doc.file_name.endsWith('.md')" class="w-4 h-4 text-sky-600 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                <svg v-else class="w-4 h-4 text-slate-400 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                <div class="min-w-0 flex-1">
                  <div
                    class="font-medium text-slate-900 group-hover:text-blue-600 transition-colors truncate"
                    :title="`${doc.file_name}\nID: ${doc.doc_id}`"
                  >
                    {{ doc.file_name }}
                  </div>
                  <div v-if="doc.tags && doc.tags.length > 0" class="text-[10px] text-slate-400 truncate mt-0.5">
                    {{ doc.tags.map((t) => `#${t}`).join(' ') }}
                  </div>
                </div>
              </div>
            </td>

            <!-- 所属知识库 -->
            <td class="py-3.5 px-4 whitespace-nowrap">
              <div class="text-slate-700 font-medium text-xs">{{ doc.kb_name || '-' }}</div>
              <div v-if="doc.department" class="text-[10px] text-slate-400 font-mono">{{ doc.department }}</div>
            </td>

            <!-- 文档有效密级 -->
            <td class="py-3.5 px-4 whitespace-nowrap">
              <ClassificationBadge :level="doc.effective_classification_level ?? doc.classification_level" />
            </td>

            <!-- 分类 -->
            <td class="py-3.5 px-4 whitespace-nowrap text-slate-500 text-xs">
              {{ doc.category || '默认' }}
            </td>

            <!-- 状态 -->
            <td class="py-3.5 px-4 whitespace-nowrap">
              <StatusTag :status="doc.status" />
            </td>

            <!-- 入库时间 -->
            <td class="py-3.5 px-4 whitespace-nowrap text-slate-400 font-mono text-xs">
              {{ formatDate(doc.created_at) }}
            </td>

            <!-- 操作 -->
            <td class="py-3.5 px-4 whitespace-nowrap text-right">
              <div class="flex items-center justify-end space-x-1">
                <button
                  @click="$emit('previewDoc', doc)"
                  class="text-slate-600 hover:text-blue-600 px-2 py-1 rounded-md text-xs font-medium hover:bg-slate-100 transition-colors cursor-pointer"
                  title="在新标签页打开原文档"
                >
                  预览
                </button>
                <span class="text-slate-200">|</span>
                <button
                  @click="$emit('viewChunks', doc)"
                  class="text-slate-600 hover:text-blue-600 px-2 py-1 rounded-md text-xs font-medium hover:bg-slate-100 transition-colors cursor-pointer"
                >
                  切片
                </button>
                <span class="text-slate-200">|</span>
                <button
                  @click="$emit('viewProgress', doc)"
                  class="text-slate-600 hover:text-slate-900 px-2 py-1 rounded-md text-xs font-medium hover:bg-slate-100 transition-colors cursor-pointer"
                >
                  进度
                </button>
                <template v-if="doc.can_manage">
                  <span class="text-slate-200">|</span>
                  <button
                    @click="$emit('reclassifyDoc', doc)"
                    class="text-indigo-600 hover:text-indigo-800 px-2 py-1 rounded-md text-xs font-medium hover:bg-indigo-50 transition-colors cursor-pointer"
                    title="调整单篇文档密级"
                  >
                    调密
                  </button>
                </template>
                <template v-if="doc.can_delete !== undefined ? doc.can_delete : canDelete">
                  <span class="text-slate-200">|</span>
                  <button
                    @click="$emit('deleteDoc', doc)"
                    class="text-slate-400 hover:text-rose-600 p-1.5 rounded-md transition-colors cursor-pointer"
                    title="删除文档"
                  >
                    <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                  </button>
                </template>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { DocumentItem } from '../../types';
import StatusTag from './StatusTag.vue';
import ClassificationBadge from '../common/ClassificationBadge.vue';

withDefaults(
  defineProps<{
    documents: DocumentItem[];
    canDelete?: boolean;
    loading?: boolean;
  }>(),
  {
    canDelete: false,
    loading: false,
  }
);

defineEmits<{
  (e: 'previewDoc', doc: DocumentItem): void;
  (e: 'viewChunks', doc: DocumentItem): void;
  (e: 'viewProgress', doc: DocumentItem): void;
  (e: 'reclassifyDoc', doc: DocumentItem): void;
  (e: 'deleteDoc', doc: DocumentItem): void;
}>();

const formatDate = (isoStr: string) => {
  if (!isoStr) return '-';
  const d = new Date(isoStr);
  return d.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
};
</script>
