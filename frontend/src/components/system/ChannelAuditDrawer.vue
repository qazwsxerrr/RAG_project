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
        <div class="flex items-center gap-2.5 min-w-0">
          <div class="w-8 h-8 rounded-lg bg-blue-50 text-blue-600 flex items-center justify-center font-bold text-xs shrink-0">
            AI
          </div>
          <div class="min-w-0">
            <h2 class="text-sm font-bold text-slate-900 flex items-center gap-2">
              <span class="truncate">{{ channel?.name }}</span>
              <span 
                class="text-[10px] px-1.5 py-0.5 rounded font-sans font-medium shrink-0"
                :class="getStateBadgeClass(channel?.state)"
              >
                {{ formatStateText(channel?.state) }}
              </span>
              <span v-if="channel?.weight" class="text-[10px] text-slate-400 font-normal shrink-0">
                权重 {{ channel.weight }}
              </span>
            </h2>
            <p class="text-[11px] text-slate-400 font-mono truncate" :title="`${channel?.model} · ${channel?.base_url}`">
              {{ channel?.model }} · {{ channel?.base_url }}
            </p>
          </div>
        </div>

        <div class="flex items-center gap-2 shrink-0">
          <!-- 重置熔断按钮：仅管理员可见，且健康状态(CLOSED)下置灰避免无意义操作 -->
          <button
            v-if="canReset"
            @click="handleReset"
            :disabled="isResetting || channel?.state === 'CLOSED'"
            class="text-xs px-2.5 py-1 rounded-lg border transition-colors flex items-center gap-1.5"
            :class="channel?.state === 'CLOSED'
              ? 'border-slate-200 text-slate-400 bg-slate-50 cursor-not-allowed'
              : 'border-amber-200 bg-amber-50 text-amber-800 hover:bg-amber-100 cursor-pointer'"
            :title="channel?.state === 'CLOSED' ? '当前节点运行正常，无需重置' : '手动恢复节点为正常状态'"
          >
            <svg class="w-3.5 h-3.5" :class="{ 'animate-spin': isResetting }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
            <span>{{ channel?.state === 'CLOSED' ? '状态正常' : '恢复状态' }}</span>
          </button>

          <!-- 关闭抽屉按钮 -->
          <button 
            @click="$emit('close')" 
            class="p-1 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100 cursor-pointer"
          >
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
      </div>

      <!-- 概览指标小卡片 -->
      <div class="grid grid-cols-4 gap-2 p-4 bg-slate-50 border-b border-slate-100 text-center font-mono">
        <div class="bg-white p-2 rounded-lg border border-slate-200/60">
          <div class="text-[10px] text-slate-400">可用率</div>
          <div class="text-xs font-bold text-emerald-600">
            {{ channel?.total_calls === 0 ? '-' : `${channel?.sla_percent}%` }}
          </div>
        </div>
        <div class="bg-white p-2 rounded-lg border border-slate-200/60">
          <div class="text-[10px] text-slate-400">首字延迟</div>
          <div class="text-xs font-bold text-slate-700">
            {{ channel?.ttft_ms ? `${channel.ttft_ms}ms` : '-' }}
          </div>
        </div>
        <div class="bg-white p-2 rounded-lg border border-slate-200/60">
          <div class="text-[10px] text-slate-400">平均耗时</div>
          <div class="text-xs font-bold text-slate-700">
            {{ channel?.avg_latency_ms ? `${channel.avg_latency_ms}ms` : '-' }}
          </div>
        </div>
        <div class="bg-white p-2 rounded-lg border border-slate-200/60">
          <div class="text-[10px] text-slate-400">P95 延迟</div>
          <div class="text-xs font-bold text-slate-700">
            {{ channel?.p95_latency_ms ? `${channel.p95_latency_ms}ms` : '-' }}
          </div>
        </div>
      </div>

      <!-- 审计日志流水列表 -->
      <div class="flex-1 overflow-y-auto p-4 space-y-2.5">
        <div class="flex items-center justify-between text-xs font-semibold text-slate-600 mb-2">
          <span>最近调用记录（{{ audits.length }} 条）</span>
        </div>

        <div v-if="isLoading" class="text-center py-10 text-xs text-slate-400">
          正在加载调用记录...
        </div>

        <div v-else-if="audits.length === 0" class="text-center py-10 text-xs text-slate-400">
          暂无调用记录
        </div>

        <div 
          v-else
          v-for="item in audits" 
          :key="item.id"
          class="p-3 rounded-lg border text-xs font-mono transition-colors"
          :class="item.status === 'SUCCESS' ? 'bg-white border-slate-200 hover:border-slate-300' : 'bg-rose-50/40 border-rose-200 hover:border-rose-300'"
        >
          <div class="flex items-center justify-between mb-1">
            <span class="font-bold font-sans flex items-center gap-1.5" :class="item.status === 'SUCCESS' ? 'text-slate-800' : 'text-rose-600'">
              <span class="w-1.5 h-1.5 rounded-full" :class="item.status === 'SUCCESS' ? 'bg-emerald-500' : 'bg-rose-500'"></span>
              <span>{{ formatActionLabel(item.action) }}</span>
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
import { ref, watch, computed } from 'vue';
import { systemApi, type ModelChannelItem, type AuditLogItem } from '../../api/system';
import { useAuthStore, PERMISSIONS } from '../../stores/auth';

const props = defineProps<{
  isOpen: boolean;
  channel: ModelChannelItem | null;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'refresh'): void;
}>();

const authStore = useAuthStore();
const canReset = computed(() => authStore.hasPermission(PERMISSIONS.SYSTEM_ADMIN));

const audits = ref<AuditLogItem[]>([]);
const isLoading = ref(false);
const isResetting = ref(false);

watch(
  () => [props.isOpen, props.channel?.name] as const,
  async ([open, nodeName]) => {
    if (open && nodeName) {
      await fetchAudits(nodeName);
    }
  },
  { immediate: true }
);

function formatStateText(state?: string): string {
  if (state === 'CLOSED') return '正常';
  if (state === 'HALF_OPEN') return '恢复探测';
  if (state === 'OPEN') return '已隔离';
  return state || '就绪';
}

function formatActionLabel(action?: string): string {
  switch (action) {
    case 'LLM_INFERENCE':
      return '模型推理调用';
    case 'LLM_FAILOVER':
      return '备用节点切换';
    case 'EMBEDDING_INFERENCE':
      return '向量计算调用';
    case 'RERANK_INFERENCE':
      return '相关性排序调用';
    case 'LLM_CIRCUIT_RESET':
      return '手动恢复状态';
    default:
      return action || '服务调用';
  }
}

function getStateBadgeClass(state?: string): string {
  if (state === 'CLOSED') return 'bg-emerald-50 text-emerald-600 border border-emerald-200/60';
  if (state === 'HALF_OPEN') return 'bg-amber-50 text-amber-600 border border-amber-200/60';
  return 'bg-rose-50 text-rose-600 border border-rose-200/60';
}

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
  } catch (err: any) {
    const msg = err?.response?.data?.detail || err?.message || '重置健康状态失败';
    alert(msg);
  } finally {
    isResetting.value = false;
  }
}
</script>
