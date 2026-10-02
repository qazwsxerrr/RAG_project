<template>
  <div v-if="thinking" class="thought-timeline mb-3 select-none">
    <!-- 顶栏：简明状态与折叠触发 (无封闭卡片框，纯净平铺) -->
    <div 
      @click="isExpanded = !isExpanded" 
      class="py-1.5 flex items-center justify-between cursor-pointer text-xs text-slate-500 hover:text-slate-800 transition-colors"
    >
      <div class="flex items-center space-x-2">
        <!-- 运行状态指示点 -->
        <span 
          v-if="thinking.total_overall_ms === 0" 
          class="flex h-1.5 w-1.5 relative"
        >
          <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75"></span>
          <span class="relative inline-flex rounded-full h-1.5 w-1.5 bg-blue-600"></span>
        </span>
        <span v-else class="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>

        <span class="font-medium text-slate-700">检索与推理流水线</span>
        
        <span v-if="thinking.total_overall_ms > 0" class="text-slate-400 font-mono text-[11px]">
          检索 {{ thinking.total_retrieve_ms }} ms · 全程 {{ thinking.total_overall_ms }} ms
        </span>
        <span v-else-if="thinking.total_retrieve_ms > 0" class="text-slate-400 font-mono text-[11px]">
          检索就绪 ({{ thinking.total_retrieve_ms }} ms) · 正在组织回答
        </span>
        <span v-else class="text-blue-600 font-mono text-[11px] animate-pulse">
          多阶段流水线推演中...
        </span>
      </div>

      <div class="flex items-center space-x-1 text-[11px] text-slate-400">
        <span>{{ isExpanded ? '收起' : '展开详情' }}</span>
        <svg 
          class="w-3 h-3 transition-transform duration-200" 
          :class="{ 'rotate-180': isExpanded }" 
          viewBox="0 0 24 24" 
          fill="none" 
          stroke="currentColor" 
          stroke-width="2"
        >
          <polyline points="6 9 12 15 18 9"/>
        </svg>
      </div>
    </div>

    <!-- 展开明细列表：靠左细线引导，彻底去除外层大闭合方框 -->
    <div v-show="isExpanded" class="pl-2.5 my-1 border-l border-slate-200 space-y-1">
      <div 
        v-for="(step, idx) in thinking.steps" 
        :key="idx" 
        class="flex items-center justify-between py-0.5 text-xs"
      >
        <!-- 左侧：状态圆点 + 阶段名称 + 说明文案 (无多余标签框) -->
        <div class="flex items-center space-x-2 min-w-0 pr-4">
          <!-- 动态微图标 -->
          <svg 
            v-if="step.status === '执行中...'" 
            class="w-3 h-3 text-blue-600 animate-spin shrink-0" 
            viewBox="0 0 24 24" 
            fill="none" 
            stroke="currentColor" 
            stroke-width="2.5"
          >
            <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
          </svg>
          <span 
            v-else 
            class="w-1.5 h-1.5 rounded-full shrink-0"
            :class="[
              step.status === '已执行' ? 'bg-emerald-500' :
              step.status.includes('熔断') || step.status.includes('跳过') ? 'bg-amber-500' :
              'bg-slate-300'
            ]"
          ></span>

          <!-- 阶段名称 -->
          <span class="font-medium text-slate-700 text-xs shrink-0">{{ step.name }}</span>

          <!-- 阶段说明文案 -->
          <span 
            v-if="step.summary" 
            class="text-slate-400 text-[11px] truncate hidden sm:inline"
            :class="{ 'text-blue-600/90 animate-pulse font-normal': step.status === '执行中...' }"
          >
            {{ step.summary }}
          </span>
        </div>

        <!-- 右侧：条数流转与精准耗时 -->
        <div class="flex items-center space-x-2 text-[11px] font-mono shrink-0">
          <span v-if="step.count_change" class="text-slate-500">
            {{ step.count_change }}
          </span>
          <span v-if="step.status === '执行中...'" class="text-blue-600 font-medium animate-pulse">
            推演中...
          </span>
          <span v-else class="text-slate-400">
            {{ step.duration_ms }} ms
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import type { ThinkingProcess } from '../../types';

defineProps<{
  thinking: ThinkingProcess | null;
}>();

// 默认直接展开，方便实时查看各阶段算法流转
const isExpanded = ref(true);
</script>
