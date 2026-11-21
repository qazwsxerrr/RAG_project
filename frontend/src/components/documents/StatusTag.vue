<template>
  <!-- 形态 1 (plain): 纯净圆点 + 文字状态标签（用于文档列表、模型通道卡片底部） -->
  <span
    v-if="variant === 'plain'"
    class="inline-flex items-center gap-1.5 text-xs font-medium select-none text-slate-700 whitespace-nowrap"
  >
    <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="tagStyle.dot"></span>
    <span :class="tagStyle.textColor">{{ displayText }}</span>
  </span>

  <!-- 形态 2 (pill): 带背景与边框的胶囊徽标（用于流水线进度顶栏、监控抽屉顶栏） -->
  <span
    v-else
    class="inline-flex items-center gap-1.5 text-xs px-2.5 py-0.5 rounded-full font-medium border select-none whitespace-nowrap"
    :class="tagStyle.pill"
  >
    <span v-if="showDot" class="w-1.5 h-1.5 rounded-full shrink-0" :class="tagStyle.dot"></span>
    <span>{{ displayText }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = withDefaults(
  defineProps<{
    status?: string;
    variant?: 'plain' | 'pill';
    customText?: string;
    showDot?: boolean;
  }>(),
  {
    status: '',
    variant: 'plain',
    customText: '',
    showDot: false,
  }
);

const tagStyle = computed(() => {
  const s = (props.status || '').toLowerCase();

  // 1. 文档入库完成 / 活跃态
  if (s === 'completed' || s === 'active' || s === 'finish') {
    return {
      text: '入库就绪',
      textColor: 'text-slate-800',
      dot: 'bg-emerald-500',
      pill: 'bg-emerald-50 text-emerald-700 border-emerald-200/80',
    };
  }
  // 2. 模型通道健康态 (CLOSED)
  if (s === 'closed') {
    return {
      text: '运行正常',
      textColor: 'text-slate-600 text-[11px]',
      dot: 'bg-emerald-500',
      pill: 'bg-emerald-50 text-emerald-600 border-emerald-200/60 text-[10px] px-1.5 py-0.5 rounded',
    };
  }
  // 3. 队列排队等待
  if (s === 'queued' || s === 'pending') {
    return {
      text: '队列排队中',
      textColor: 'text-indigo-600',
      dot: 'bg-indigo-500 animate-pulse',
      pill: 'bg-indigo-50 text-indigo-700 border-indigo-200/80 animate-pulse',
    };
  }
  // 4. 流水线运行处理中
  if (s === 'running' || s === 'process' || s === 'ingested') {
    return {
      text: '处理中',
      textColor: 'text-blue-600',
      dot: 'bg-blue-500 animate-pulse',
      pill: 'bg-blue-50 text-blue-700 border-blue-200/80',
    };
  }
  // 5. 待人工审核或模型通道半开恢复探测 (HALF_OPEN)
  if (s === 'pending_review' || s === 'half_open') {
    const isHalfOpen = s === 'half_open';
    return {
      text: isHalfOpen ? '恢复探测中' : '待审核',
      textColor: isHalfOpen ? 'text-slate-600 text-[11px]' : 'text-amber-600',
      dot: 'bg-amber-400',
      pill: isHalfOpen
        ? 'bg-amber-50 text-amber-600 border-amber-200/60 text-[10px] px-1.5 py-0.5 rounded'
        : 'bg-amber-50 text-amber-800 border-amber-200/80 animate-pulse',
    };
  }
  // 6. 防重相似文档预警
  if (s === 'duplicate_warning') {
    return {
      text: '防重预警',
      textColor: 'text-orange-600',
      dot: 'bg-orange-500',
      pill: 'bg-orange-50 text-orange-800 border-orange-200/80 animate-pulse',
    };
  }
  // 7. 失败异常或模型通道熔断隔离 (OPEN)
  if (s === 'failed' || s === 'error' || s === 'open') {
    const isOpen = s === 'open';
    return {
      text: isOpen ? '熔断隔离' : '异常',
      textColor: isOpen ? 'text-slate-600 text-[11px]' : 'text-rose-600',
      dot: isOpen ? 'bg-rose-500 animate-pulse' : 'bg-rose-500',
      pill: isOpen
        ? 'bg-rose-50 text-rose-600 border-rose-200/60 text-[10px] px-1.5 py-0.5 rounded'
        : 'bg-rose-50 text-rose-700 border-rose-200/80',
    };
  }
  // 8. 已取消
  if (s === 'cancelled') {
    return {
      text: '已取消',
      textColor: 'text-slate-400',
      dot: 'bg-slate-300',
      pill: 'bg-slate-100 text-slate-600 border-slate-200',
    };
  }
  return {
    text: props.status || '就绪',
    textColor: 'text-slate-500',
    dot: 'bg-slate-400',
    pill: 'bg-slate-100 text-slate-600 border-slate-200',
  };
});

const displayText = computed(() => {
  return props.customText && props.customText.trim() ? props.customText.trim() : tagStyle.value.text;
});
</script>
