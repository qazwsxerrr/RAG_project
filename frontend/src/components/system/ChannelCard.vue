<template>
  <div 
    @click="$emit('select', channel)"
    class="channel-card bg-white rounded-xl border border-slate-200/80 p-4 transition-all duration-200 hover:shadow-md hover:border-slate-300 cursor-pointer flex flex-col justify-between group/card"
  >
    <!-- 卡片顶部：图标、模型标识与角色权重标签 -->
    <div>
      <div class="flex items-start justify-between gap-2 mb-3">
        <div class="flex items-center gap-2.5 min-w-0">
          <!-- 厂商/模型矢量徽标 -->
          <div class="w-8 h-8 rounded-lg flex items-center justify-center shrink-0" :class="getIconBgClass(channel)">
            <svg v-if="channel.provider === 'kedaya' || channel.name.includes('grok')" class="w-4 h-4 text-purple-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="10"/><path d="m4.93 4.93 14.14 14.14"/></svg>
            <svg v-else-if="channel.model.includes('deepseek')" class="w-4 h-4 text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 16.1A5 5 0 0 1 5.9 14h12.2a5 5 0 0 1 3.9 2.1"/><path d="M12 2v10"/><circle cx="12" cy="7" r="3"/></svg>
            <svg v-else-if="channel.model.includes('qwen')" class="w-4 h-4 text-teal-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 22 8.5 22 15.5 12 22 2 15.5 2 8.5 12 2"/><line x1="12" y1="22" x2="12" y2="15.5"/><polyline points="22 8.5 12 15.5 2 8.5"/></svg>
            <svg v-else class="w-4 h-4 text-slate-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
          </div>

          <!-- 模型标题与子信息 -->
          <div class="min-w-0">
            <h3 class="text-xs font-bold text-slate-800 truncate group-hover/card:text-blue-600 transition-colors">
              {{ formatDisplayName(channel) }}
            </h3>
            <p class="text-[10px] text-slate-400 font-mono truncate" :title="channel.model">
              {{ channel.model }}
            </p>
          </div>
        </div>

        <!-- 角色与权重标签 -->
        <div class="flex items-center gap-1 shrink-0">
          <span 
            class="text-[9px] font-medium px-1.5 py-0.5 rounded"
            :class="getRoleTagClass(channel.role)"
          >
            {{ getRoleLabel(channel.role) }}
          </span>
          <span class="text-[9px] text-slate-400 font-mono">
            {{ channel.weight }}w
          </span>
        </div>
      </div>

      <!-- 三列关键数据指标 (可用率 / 首TOKEN / 平均延迟) -->
      <div class="grid grid-cols-3 gap-2 py-2 mb-3 border-y border-slate-100 text-center">
        <!-- 可用率 SLA% -->
        <div class="space-y-0.5">
          <div class="text-[10px] text-slate-400">可用率</div>
          <div class="text-sm font-bold font-mono" :class="getSlaColorClass(channel.sla_percent, channel.total_calls)">
            {{ channel.total_calls === 0 ? '-' : `${channel.sla_percent}%` }}
          </div>
        </div>

        <!-- 首 TOKEN (TTFT) -->
        <div class="space-y-0.5">
          <div class="text-[10px] text-slate-400">首 TOKEN</div>
          <div class="text-sm font-bold font-mono text-slate-700">
            {{ formatLatency(channel.ttft_ms) }}
          </div>
        </div>

        <!-- 平均耗时 -->
        <div class="space-y-0.5">
          <div class="text-[10px] text-slate-400">平均延迟</div>
          <div class="text-sm font-bold font-mono text-slate-600">
            {{ formatLatency(channel.avg_latency_ms) }}
          </div>
        </div>
      </div>

      <!-- 嵌入 UptimeBar 状态条 -->
      <UptimeBar :records="channel.recent_bars" :countdown-text="countdownText" />
    </div>

    <!-- 卡片底部元信息 -->
    <div class="pt-3 mt-3 border-t border-slate-100/70 flex items-center justify-between text-[10px] text-slate-400 font-mono">
      <div class="flex items-center gap-1.5">
        <span class="w-1.5 h-1.5 rounded-full" :class="getStateDotClass(channel.state)"></span>
        <span>{{ formatStateName(channel.state) }}</span>
      </div>
      <div class="truncate max-w-[120px]" :title="channel.masked_key">
        {{ channel.masked_key }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import UptimeBar from './UptimeBar.vue';
import type { ModelChannelItem } from '../../api/system';

const props = defineProps<{
  channel: ModelChannelItem;
  countdownText?: string;
}>();

defineEmits<{
  (e: 'select', channel: ModelChannelItem): void;
}>();

function getRoleLabel(role: string): string {
  if (role === 'PRIMARY') return '主模型';
  if (role === 'INFRA') return '检索基建';
  return '备用';
}

function getRoleTagClass(role: string): string {
  if (role === 'PRIMARY') return 'bg-blue-50 text-blue-600 border border-blue-200/60';
  if (role === 'INFRA') return 'bg-purple-50 text-purple-600 border border-purple-200/60';
  return 'bg-slate-100 text-slate-600';
}

function formatDisplayName(c: ModelChannelItem): string {
  if (c.role === 'INFRA') {
    return c.name;
  }
  if (c.role === 'PRIMARY') {
    return `${c.name} (主节点)`;
  }
  if (c.name.includes('deepseek')) {
    return 'DeepSeek-V3 (备用)';
  }
  if (c.name.includes('qwen-vl') || c.name.includes('vl')) {
    return 'Qwen-VL-72B (视觉备用)';
  }
  if (c.name.includes('qwen')) {
    return 'Qwen-2.5-72B (备用)';
  }
  return `${c.name} (备用)`;
}

function getIconBgClass(c: ModelChannelItem): string {
  if (c.provider === 'kedaya') return 'bg-purple-50';
  if (c.model.includes('deepseek')) return 'bg-blue-50';
  if (c.model.includes('qwen')) return 'bg-teal-50';
  return 'bg-slate-100';
}

function getSlaColorClass(sla: number, totalCalls: number): string {
  if (totalCalls === 0) return 'text-slate-400';
  if (sla >= 99.0) return 'text-emerald-600';
  if (sla >= 95.0) return 'text-teal-600';
  if (sla >= 85.0) return 'text-amber-500';
  return 'text-rose-600';
}

function formatLatency(ms: number): string {
  if (!ms || ms <= 0) return '-';
  if (ms >= 1000) {
    return (ms / 1000).toFixed(1) + 's';
  }
  return `${ms}ms`;
}

function getStateDotClass(state: string): string {
  if (state === 'CLOSED') return 'bg-emerald-500';
  if (state === 'OPEN') return 'bg-rose-500 animate-pulse';
  if (state === 'HALF_OPEN') return 'bg-amber-400';
  return 'bg-slate-300';
}

function formatStateName(state: string): string {
  if (state === 'CLOSED') return '健康运转';
  if (state === 'OPEN') return '熔断隔离中';
  if (state === 'HALF_OPEN') return '探测自愈中';
  return '就绪';
}
</script>
