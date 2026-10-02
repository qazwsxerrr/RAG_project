<template>
  <span class="inline-flex items-center gap-1.5 text-xs font-medium select-none text-slate-700 whitespace-nowrap">
    <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="tagStyle.dot"></span>
    <span :class="tagStyle.textColor">{{ tagStyle.text }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
  status: string;
}>();

const tagStyle = computed(() => {
  const s = props.status?.toLowerCase() || '';
  if (s === 'completed' || s === 'active' || s === 'finish') {
    return {
      text: '入库就绪',
      textColor: 'text-slate-800',
      dot: 'bg-emerald-500',
    };
  }
  if (s === 'running' || s === 'process' || s === 'ingested') {
    return {
      text: '处理中',
      textColor: 'text-blue-600',
      dot: 'bg-blue-500 animate-pulse',
    };
  }
  if (s === 'pending_review') {
    return {
      text: '待审核',
      textColor: 'text-amber-600',
      dot: 'bg-amber-500',
    };
  }
  if (s === 'duplicate_warning') {
    return {
      text: '防重预警',
      textColor: 'text-orange-600',
      dot: 'bg-orange-500',
    };
  }
  if (s === 'failed' || s === 'error') {
    return {
      text: '异常',
      textColor: 'text-rose-600',
      dot: 'bg-rose-500',
    };
  }
  if (s === 'cancelled') {
    return {
      text: '已取消',
      textColor: 'text-slate-400',
      dot: 'bg-slate-300',
    };
  }
  return {
    text: props.status || '未入库',
    textColor: 'text-slate-500',
    dot: 'bg-slate-400',
  };
});
</script>
