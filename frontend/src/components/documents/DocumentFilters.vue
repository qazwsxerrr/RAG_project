<template>
  <div class="rounded-xl px-3 py-1.5 border border-slate-200 bg-white flex flex-wrap items-center gap-2 shadow-sm">
    <!-- 搜索输入 -->
    <div class="relative flex-1 min-w-[200px] flex items-center">
      <svg class="w-3.5 h-3.5 text-slate-400 absolute left-1 pointer-events-none" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input
        v-model="filters.keyword"
        type="text"
        placeholder="搜索文档名称 / 知识库 / 标签..."
        class="w-full text-xs pl-6 pr-2 py-1 bg-transparent text-slate-800 placeholder-slate-400 outline-none"
      />
    </div>

    <div class="h-4 w-px bg-slate-200 hidden sm:block"></div>

    <!-- 知识库筛选 -->
    <div class="relative">
      <select
        v-model="filters.kbId"
        class="text-xs py-1 pl-1 pr-2 bg-transparent text-slate-600 hover:text-slate-900 outline-none cursor-pointer border-0 font-medium max-w-[180px]"
      >
        <option value="">全部授权知识库</option>
        <option v-for="kb in knowledgeBases" :key="kb.id" :value="kb.id">
          {{ kb.name }}
        </option>
      </select>
    </div>

    <div class="h-4 w-px bg-slate-200 hidden sm:block"></div>

    <!-- 密级筛选 -->
    <div class="relative">
      <select
        v-model="filters.classificationLevel"
        class="text-xs py-1 pl-1 pr-2 bg-transparent text-slate-600 hover:text-slate-900 outline-none cursor-pointer border-0 font-medium"
      >
        <option value="">全部密级</option>
        <option value="1">L1 全员公开</option>
        <option value="2">L2 部门内部</option>
        <option value="3">L3 管理层机密</option>
        <option value="4">L4 核心绝密</option>
      </select>
    </div>

    <div class="h-4 w-px bg-slate-200 hidden sm:block"></div>

    <!-- 分类筛选 -->
    <div class="relative">
      <select
        v-model="filters.category"
        class="text-xs py-1 pl-1 pr-2 bg-transparent text-slate-600 hover:text-slate-900 outline-none cursor-pointer border-0 font-medium"
      >
        <option value="">全部分类</option>
        <option v-for="c in categoryOptions" :key="c" :value="c">{{ c }}</option>
      </select>
    </div>

    <div class="h-4 w-px bg-slate-200 hidden sm:block"></div>

    <!-- 状态筛选 -->
    <div class="relative">
      <select
        v-model="filters.status"
        class="text-xs py-1 pl-1 pr-2 bg-transparent text-slate-600 hover:text-slate-900 outline-none cursor-pointer border-0 font-medium"
      >
        <option value="">全部状态</option>
        <option value="queued">队列排队中</option>
        <option value="completed">入库完成</option>
        <option value="running">入库处理中</option>
        <option value="pending_review">待人工审核</option>
        <option value="failed">异常失败</option>
      </select>
    </div>

    <!-- 重置按钮 -->
    <button
      v-if="hasActiveFilters"
      @click="resetFilters"
      class="text-xs text-rose-500 hover:text-rose-700 px-2 py-1 transition-colors font-medium ml-auto cursor-pointer"
    >
      清空筛选
    </button>
  </div>
</template>

<script setup lang="ts">
import { reactive, computed, watch } from 'vue';
import type { AccessibleKnowledgeBase } from '../../types';

defineProps<{
  knowledgeBases: AccessibleKnowledgeBase[];
  categoryOptions: string[];
}>();

const emit = defineEmits<{
  (
    e: 'change',
    filters: {
      keyword: string;
      kbId: string;
      classificationLevel: string;
      category: string;
      status: string;
    }
  ): void;
}>();

const filters = reactive({
  keyword: '',
  kbId: '',
  classificationLevel: '',
  category: '',
  status: '',
});

const hasActiveFilters = computed(() =>
  Boolean(
    filters.keyword ||
      filters.kbId ||
      filters.classificationLevel ||
      filters.category ||
      filters.status
  )
);

watch(
  filters,
  (newVal) => {
    emit('change', { ...newVal });
  },
  { deep: true }
);

const resetFilters = () => {
  filters.keyword = '';
  filters.kbId = '';
  filters.classificationLevel = '';
  filters.category = '';
  filters.status = '';
};
</script>
