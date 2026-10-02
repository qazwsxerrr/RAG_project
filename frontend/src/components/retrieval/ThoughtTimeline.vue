<template>
  <div v-if="thinking" class="thought-timeline mb-3 select-none">
    <!-- 顶栏：简明状态与折叠触发 (无文字，仅保留上下旋转箭头) -->
    <div 
      @click="isExpanded = !isExpanded" 
      class="py-1.5 flex items-center justify-between cursor-pointer text-xs text-slate-500 hover:text-slate-800 transition-colors group"
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

      <!-- 右侧仅保留箭头图标，去除任何汉字提示 -->
      <div 
        class="p-1 rounded text-slate-400 group-hover:text-slate-700 transition-colors"
        :title="isExpanded ? '收起' : '展开详情'"
      >
        <svg 
          class="w-3.5 h-3.5 transition-transform duration-200" 
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

    <!-- 展开明细列表：靠左细线引导，随算法流转逐行自上而下平滑流动生长 -->
    <div v-show="isExpanded" class="pl-2.5 my-1 border-l-2 border-slate-200/90 space-y-1.5">
      <TransitionGroup name="pipeline-step">
        <div 
          v-for="step in activeSteps" 
          :key="step.name" 
          class="text-xs py-0.5"
        >
          <!-- 阶段第一行：点 + 阶段名 + 状态徽标 ─── 耗时 + 数量变化 -->
          <div class="flex items-center justify-between">
            <div class="flex items-center space-x-2 min-w-0 pr-3">
              <!-- 动态指示点 -->
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
              <span class="font-medium text-slate-800 text-xs shrink-0">{{ step.name }}</span>

              <!-- 状态徽标：已执行 采用清晰的翠绿色字样 -->
              <span 
                v-if="step.status === '已执行'" 
                class="text-[11px] text-emerald-600 font-medium shrink-0"
              >已执行</span>
              <span 
                v-else-if="step.status === '执行中...'" 
                class="text-[11px] text-blue-600 font-medium shrink-0 animate-pulse"
              >执行中...</span>
              <span 
                v-else-if="step.status.includes('熔断') || step.status.includes('跳过')" 
                class="text-[11px] text-amber-600 font-medium shrink-0"
              >{{ step.status }}</span>
            </div>

            <!-- 右侧：耗时与数量流转 -->
            <div class="flex items-center space-x-3 text-[11px] font-mono shrink-0">
              <span v-if="step.duration_ms > 0 || step.status === '已执行'" class="text-slate-400">
                {{ step.duration_ms }} ms
              </span>
              <span v-if="step.count_change" class="text-slate-500 font-medium">
                {{ step.count_change }}
              </span>
            </div>
          </div>

          <!-- 阶段第二行：两级说明文案 (如果有细节摘要，缩进独立换行展示，对齐参考图) -->
          <div 
            v-if="step.summary && step.status !== '执行中...'" 
            class="pl-3.5 mt-0.5 text-[11px] text-slate-400 leading-normal"
          >
            {{ step.summary }}
          </div>
        </div>
      </TransitionGroup>

      <!-- 底部活跃状态流动提示 (对齐参考图: ••• 正在检索知识库...) -->
      <div 
        v-if="isPipelineActive" 
        class="flex items-center space-x-2 text-[11px] text-blue-600 py-1 pl-0.5 font-sans"
      >
        <span class="inline-flex space-x-1 items-center">
          <span class="w-1 h-1 rounded-full bg-blue-500 animate-bounce"></span>
          <span class="w-1 h-1 rounded-full bg-blue-500 animate-bounce [animation-delay:0.15s]"></span>
          <span class="w-1 h-1 rounded-full bg-blue-500 animate-bounce [animation-delay:0.3s]"></span>
        </span>
        <span class="font-normal">{{ activePromptText }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import type { ThinkingProcess } from '../../types';

const props = defineProps<{
  thinking: ThinkingProcess | null;
}>();

// 默认直接展开，方便实时查看各阶段算法流转
const isExpanded = ref(true);

// 过滤掉所有未开始的占位，仅展示正在运行或已执行完成的有效阶段
const activeSteps = computed(() => {
  if (!props.thinking?.steps) return [];
  return props.thinking.steps.filter((s) => s.status !== '等待中');
});

// 判断流水线是否仍在活跃推进中
const isPipelineActive = computed(() => {
  if (!props.thinking) return false;
  if (props.thinking.total_overall_ms === 0) return true;
  return activeSteps.value.some((s) => s.status === '执行中...');
});

const activePromptText = computed(() => {
  const currentRunning = activeSteps.value.find((s) => s.status === '执行中...');
  if (currentRunning) {
    return `正在进行${currentRunning.name}...`;
  }
  return '正在检索多阶段知识库...';
});
</script>

<style scoped>
.pipeline-step-enter-active,
.pipeline-step-leave-active {
  transition: all 0.25s ease-out;
}
.pipeline-step-enter-from {
  opacity: 0;
  transform: translateY(-4px);
}
.pipeline-step-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
