<template>
  <div v-if="isOpen" class="fixed inset-0 z-50 overflow-hidden flex justify-end">
    <!-- 遮罩背景 -->
    <div 
      @click="$emit('close')" 
      class="fixed inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity"
    ></div>

    <!-- 右侧滑出抽屉容器 -->
    <div class="relative w-full max-w-xl bg-white shadow-2xl flex flex-col h-full z-10 animate-in slide-in-from-right duration-200 border-l border-slate-200">
      
      <!-- 抽屉头部 -->
      <div class="p-4 border-b border-slate-100 flex items-center justify-between bg-slate-50/50">
        <div class="flex items-center gap-2.5">
          <div class="w-8 h-8 rounded-lg bg-blue-50 text-blue-600 flex items-center justify-center font-bold text-xs">
            AI
          </div>
          <div>
            <h2 class="text-sm font-bold text-slate-900 flex items-center gap-2">
              <span>{{ channel?.name }}</span>
              <span 
                class="text-[10px] px-1.5 py-0.5 rounded font-mono font-normal"
                :class="channel?.state === 'CLOSED' ? 'bg-emerald-50 text-emerald-600 border border-emerald-200/60' : 'bg-rose-50 text-rose-600 border border-rose-200/60'"
              >
                {{ channel?.state }}
              </span>
            </h2>
            <p class="text-[11px] text-slate-400 font-mono">{{ channel?.model }} · {{ channel?.base_url }}</p>
          </div>
        </div>

        <div class="flex items-center gap-2">
          <!-- 重置熔断按钮 -->
          <button
            @click="handleReset"
            :disabled="isResetting"
            class="text-xs px-2.5 py-1 rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-100 transition-colors flex items-center gap-1.5"
            title="手动将状态重置为 CLOSED"
          >
            <svg class="w-3.5 h-3.5" :class="{ 'animate-spin': isResetting }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
            <span>重置健康状态</span>
          </button>

          <!-- 关闭抽屉按钮 -->
          <button 
            @click="$emit('close')" 
            class="p-1 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100"
          >
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
      </div>

      <!-- 概览指标小卡片 -->
      <div class="grid grid-cols-4 gap-2 p-4 bg-slate-50 border-b border-slate-100 text-center font-mono">
        <div class="bg-white p-2 rounded-lg border border-slate-200/60">
          <div class="text-[10px] text-slate-400">可用率</div>
          <div class="text-xs font-bold text-emerald-600">{{ channel?.sla_percent }}%</div>
        </div>
        <div class="bg-white p-2 rounded-lg border border-slate-200/60">
          <div class="text-[10px] text-slate-400">首字延迟</div>
          <div class="text-xs font-bold text-slate-700">{{ channel?.ttft_ms }}ms</div>
        </div>
        <div class="bg-white p-2 rounded-lg border border-slate-200/60">
          <div class="text-[10px] text-slate-400">平均耗时</div>
          <div class="text-xs font-bold text-slate-700">{{ channel?.avg_latency_ms }}ms</div>
        </div>
        <div class="bg-white p-2 rounded-lg border border-slate-200/60">
          <div class="text-[10px] text-slate-400">P95 延迟</div>
          <div class="text-xs font-bold text-slate-700">{{ channel?.p95_latency_ms }}ms</div>
        </div>
      </div>

      <!-- 审计日志流水列表 -->
      <div class="flex-1 overflow-y-auto p-4 space-y-2.5">
        <div class="flex items-center justify-between text-xs font-semibold text-slate-600 mb-2">
          <span>历次真实调用流水 (近 {{ audits.length }} 条)</span>
          <span class="text-[10px] text-slate-400 font-normal">基于 sys_audit_logs</span>
        </div>

        <div v-if="isLoading" class="text-center py-10 text-xs text-slate-400">
          正在加载审计记录...
        </div>

        <div v-else-if="audits.length === 0" class="text-center py-10 text-xs text-slate-400">
          暂无历史审计调用记录
        </div>

        <div 
          v-else
          v-for="item in audits" 
          :key="item.id"
          class="p-3 rounded-lg border text-xs font-mono transition-colors"
          :class="item.status === 'SUCCESS' ? 'bg-white border-slate-200 hover:border-slate-300' : 'bg-rose-50/40 border-rose-200 hover:border-rose-300'"
        >
          <div class="flex items-center justify-between mb-1">
            <span class="font-bold flex items-center gap-1.5" :class="item.status === 'SUCCESS' ? 'text-slate-800' : 'text-rose-600'">
              <span class="w-1.5 h-1.5 rounded-full" :class="item.status === 'SUCCESS' ? 'bg-emerald-500' : 'bg-rose-500'"></span>
              <span>{{ item.action }}</span>
            </span>
            <span class="text-[10px] text-slate-400">{{ item.created_at }}</span>
          </div>

          <div class="flex items-center justify-between text-[11px] text-slate-500 mb-1">
            <span class="truncate max-w-[280px]" :title="item.request_id">
              {{ item.request_id }}
            </span>
            <span class="font-bold" :class="item.duration_ms > 2000 ? 'text-amber-500' : 'text-slate-600'">
              耗时: {{ item.duration_ms }}ms
            </span>
          </div>

          <div v-if="item.error_msg" class="mt-1.5 p-2 bg-rose-100/60 text-rose-800 text-[10px] rounded break-all font-sans">
            {{ item.error_msg }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { systemApi, type ModelChannelItem, type AuditLogItem } from '../../api/system';

const props = defineProps<{
  isOpen: boolean;
  channel: ModelChannelItem | null;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'refresh'): void;
}>();

const audits = ref<AuditLogItem[]>([]);
const isLoading = ref(false);
const isResetting = ref(false);

watch(
  () => props.channel,
  async (newChannel) => {
    if (newChannel && props.isOpen) {
      await fetchAudits(newChannel.name);
    }
  },
  { immediate: true }
);

async function fetchAudits(nodeName: string) {
  isLoading.value = true;
  try {
    const res = await systemApi.getChannelAudits(nodeName);
    audits.value = res.items || [];
  } catch (e) {
    audits.value = [];
  } finally {
    isLoading.value = false;
  }
}

async function handleReset() {
  if (!props.channel) return;
  isResetting.value = true;
  try {
    await systemApi.resetChannel(props.channel.name);
    emit('refresh');
    await fetchAudits(props.channel.name);
  } finally {
    isResetting.value = false;
  }
}
</script>
