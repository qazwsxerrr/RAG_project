<template>
  <div class="glass-panel rounded-2xl overflow-hidden border border-slate-200/90 bg-white shadow-sm">
    <div class="overflow-x-auto">
      <table class="w-full text-left text-xs text-slate-700">
        <!-- 表头 (浅灰微质感，单行展示) -->
        <thead class="bg-slate-50/90 text-slate-500 font-semibold border-b border-slate-200 uppercase tracking-wider text-[11px] whitespace-nowrap">
          <tr>
            <th class="py-3.5 px-4">文档名称</th>
            <th class="py-3.5 px-4">归属部门</th>
            <th class="py-3.5 px-4">分类</th>
            <th class="py-3.5 px-4">标签</th>
            <th class="py-3.5 px-4">状态</th>
            <th class="py-3.5 px-4">入库时间</th>
            <th class="py-3.5 px-4 text-right">操作</th>
          </tr>
        </thead>

        <!-- 表体 -->
        <tbody class="divide-y divide-slate-100 font-normal">
          <tr v-if="documents.length === 0">
            <td colspan="7" class="py-12 text-center text-slate-400">
              暂未收录符合条件的文档资产
            </td>
          </tr>

          <tr 
            v-for="doc in documents" 
            :key="doc.doc_id"
            class="hover:bg-slate-50/80 transition-colors group"
          >
            <!-- 文档名称 + 图标 (长文件名加省略号与提示，一行展示) -->
            <td class="py-3.5 px-4 whitespace-nowrap">
              <div class="flex items-center space-x-2.5 min-w-0 max-w-xs md:max-w-sm lg:max-w-md">
                <svg v-if="doc.file_name.endsWith('.pdf')" class="w-4 h-4 text-rose-500 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                <svg v-else-if="doc.file_name.endsWith('.md')" class="w-4 h-4 text-sky-600 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                <svg v-else class="w-4 h-4 text-slate-400 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                <div class="min-w-0 flex-1">
                  <div 
                    class="font-medium text-slate-900 group-hover:text-blue-600 transition-colors truncate"
                    :title="doc.file_name"
                  >
                    {{ doc.file_name }}
                  </div>
                  <div class="text-[10px] text-slate-400 font-mono mt-0.5 truncate max-w-[220px]" :title="'ID: ' + doc.doc_id">
                    ID: {{ doc.doc_id }}
                  </div>
                </div>
              </div>
            </td>

            <!-- 部门 (单行) -->
            <td class="py-3.5 px-4 whitespace-nowrap">
              <span class="text-slate-700 font-medium text-xs">
                {{ doc.department || '-' }}
              </span>
            </td>

            <!-- 分类 (单行) -->
            <td class="py-3.5 px-4 whitespace-nowrap text-slate-500 text-xs">
              {{ doc.category || '默认' }}
            </td>

            <!-- 标签 (方案 A：单行自然完整平铺，不截断切字) -->
            <td class="py-3.5 px-4 whitespace-nowrap">
              <div class="flex items-center gap-x-1.5 text-xs text-slate-500 whitespace-nowrap" :title="doc.tags?.join(' · ') || ''">
                <template v-for="(t, i) in doc.tags" :key="i">
                  <span>{{ t }}</span>
                  <span v-if="i < doc.tags.length - 1" class="text-slate-300">·</span>
                </template>
                <span v-if="!doc.tags || doc.tags.length === 0" class="text-slate-300">-</span>
              </div>
            </td>

            <!-- 状态 (单行) -->
            <td class="py-3.5 px-4 whitespace-nowrap">
              <StatusTag :status="doc.status" />
            </td>

            <!-- 入库时间 (单行) -->
            <td class="py-3.5 px-4 whitespace-nowrap text-slate-400 font-mono text-xs">
              {{ formatDate(doc.created_at) }}
            </td>

            <!-- 操作 (单行靠右) -->
            <td class="py-3.5 px-4 whitespace-nowrap text-right">
              <div class="flex items-center justify-end space-x-1">
                <button 
                  @click="$emit('viewChunks', doc)"
                  class="text-slate-600 hover:text-blue-600 px-2 py-1 rounded-md text-xs font-medium hover:bg-slate-100 transition-colors"
                >
                  切片详情
                </button>
                <span class="text-slate-200">|</span>
                <button 
                  @click="$emit('viewProgress', doc)"
                  class="text-slate-600 hover:text-slate-900 px-2 py-1 rounded-md text-xs font-medium hover:bg-slate-100 transition-colors"
                >
                  流水线
                </button>
                <span class="text-slate-200">|</span>
                <button 
                  @click="$emit('deleteDoc', doc)"
                  class="text-slate-400 hover:text-rose-600 p-1.5 rounded-md transition-colors"
                  title="删除此文档及所有切片"
                >
                  <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                </button>
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

defineProps<{
  documents: DocumentItem[];
}>();

defineEmits<{
  (e: 'viewChunks', doc: DocumentItem): void;
  (e: 'viewProgress', doc: DocumentItem): void;
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
