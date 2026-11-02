<template>
  <div class="uptime-bar-container w-full select-none">
    <!-- 顶部状态信息与倒计时 -->
    <div class="flex items-center justify-between text-[11px] text-slate-400 font-mono mb-1.5">
      <span>近 18 次记录</span>
      <span class="text-slate-400/80">{{ countdownText }}</span>
    </div>

    <!-- 18 根经典 Uptime 状态柱状条 (对齐 Uptime-Kuma 与参考图) -->
    <div class="flex items-end gap-1 h-7 w-full py-0.5">
      <div
        v-for="(bar, idx) in normalizedBars"
        :key="idx"
        @mouseenter="handleMouseEnter(idx, bar)"
        @mouseleave="handleMouseLeave"
        class="flex-1 rounded-[2px] transition-all duration-150 cursor-pointer relative"
        :class="[
          getBarColor(bar),
          bar.isEmpty ? 'h-3.5 opacity-40' : (activeHoverIdx === idx ? 'h-6 shadow-sm ring-1 ring-white/60' : 'h-5 hover:h-6')
        ]"
      >
        <!-- 悬停 Tooltip 浮层：由 activeHoverIdx 单一状态独占控制，彻底杜绝多柱重叠穿透 -->
        <div
          v-if="!bar.isEmpty && activeHoverIdx === idx"
          class="absolute bottom-full mb-1.5 flex flex-col items-center z-50 pointer-events-none"
          :class="getTooltipPositionClass(idx)"
        >
          <div class="bg-slate-900/95 text-white text-[10px] rounded px-2.5 py-1.5 shadow-xl whitespace-nowrap border border-slate-700/60 leading-relaxed font-sans">
            <div class="font-semibold flex items-center gap-1.5">
              <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="bar.status === 'SUCCESS' ? 'bg-emerald-400' : 'bg-rose-400'"></span>
              <span>{{ bar.status === 'SUCCESS' ? '调用成功 200 OK' : '调用异常 / 故障转移' }}</span>
            </div>
            <div class="text-slate-300 font-mono">耗时: {{ bar.duration_ms }}ms<span v-if="bar.ttft_ms"> · 首字: {{ bar.ttft_ms }}ms</span></div>
            <div class="text-slate-400 font-mono text-[9px]">{{ bar.time_str }}</div>
          </div>
          <div 
            class="w-1.5 h-1.5 bg-slate-900 rotate-45 -mt-1 border-r border-b border-slate-700/60"
            :class="getArrowPositionClass(idx)"
          ></div>
        </div>
      </div>
    </div>

    <!-- 底部时间轴指示 -->
    <div class="flex items-center justify-between text-[9px] text-slate-400/70 font-mono mt-1">
      <span>PAST</span>
      <span>NOW</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import type { RecentBarItem } from '../../api/system';

interface ExtendedBarItem extends Partial<RecentBarItem> {
  isEmpty?: boolean;
}

const activeHoverIdx = ref<number | null>(null);

function handleMouseEnter(idx: number, bar: ExtendedBarItem) {
  if (!bar.isEmpty) {
    activeHoverIdx.value = idx;
  }
}

function handleMouseLeave() {
  activeHoverIdx.value = null;
}

function getTooltipPositionClass(idx: number): string {
  // 左边缘前两根柱子：靠左对齐，防止突破卡片左边缘
  if (idx <= 2) {
    return 'left-0';
  }
  // 右边缘最后两根柱子：靠右对齐，防止突破卡片右边缘
  if (idx >= 15) {
    return 'right-0';
  }
  // 中间柱子：居中对齐
  return 'left-1/2 -translate-x-1/2';
}

function getArrowPositionClass(idx: number): string {
  if (idx <= 2) {
    return 'self-start ml-2';
  }
  if (idx >= 15) {
    return 'self-end mr-2';
  }
  return 'self-center';
}

const props = withDefaults(
  defineProps<{
    records?: RecentBarItem[];
    countdownText?: string;
  }>(),
  {
    records: () => [],
    countdownText: '实时刷新'
  }
);

// 归一化为标准的 18 根状态条，若数据不足 18 条则在左侧补齐灰色占位条
const normalizedBars = computed<ExtendedBarItem[]>(() => {
  const targetCount = 18;
  const list = [...props.records];
  if (list.length >= targetCount) {
    return list.slice(-targetCount);
  }
  const placeholdersCount = targetCount - list.length;
  const placeholders: ExtendedBarItem[] = Array.from({ length: placeholdersCount }, () => ({
    isEmpty: true
  }));
  return [...placeholders, ...list];
});

function getBarColor(bar: ExtendedBarItem): string {
  if (bar.isEmpty) {
    return 'bg-slate-200 dark:bg-slate-700';
  }
  if (bar.status === 'SUCCESS') {
    // 成功但延迟高于 2.5 秒显示高延迟预警黄，正常显示清新绿
    if (bar.duration_ms && bar.duration_ms > 2500) {
      return 'bg-amber-400 hover:bg-amber-300';
    }
    return 'bg-emerald-500 hover:bg-emerald-400';
  }
  if (bar.status === 'SWITCHED') {
    return 'bg-amber-400 hover:bg-amber-300';
  }
  // 失败或 429/5xx 熔断为艳红色
  return 'bg-rose-500 hover:bg-rose-400';
}
</script>
