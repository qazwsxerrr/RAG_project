<template>
  <div class="channel-status-view flex-1 h-full overflow-y-auto bg-[#FAFBFD] p-6 lg:p-8">
    <div class="max-w-7xl mx-auto space-y-6">
      
      <!-- 1. 顶部全局看板 (100% 对齐参考图: 渠道状态 + 时间粒度切换 + 汇总指标) -->
      <header class="bg-white rounded-xl border border-slate-200/80 p-5 shadow-xs">
        <div class="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4 border-b border-slate-100">
          
          <!-- 标题与实时更新状态 -->
          <div class="flex items-center gap-3">
            <div class="w-8 h-8 rounded-lg bg-blue-50 text-blue-600 flex items-center justify-center">
              <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
                <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>
              </svg>
            </div>
            <div>
              <div class="flex items-center gap-2">
                <h1 class="text-base font-bold text-slate-900 tracking-tight">渠道状态</h1>
                <span class="w-2 h-2 rounded-full animate-pulse" :class="overallStatusDotClass" :title="overallStatusText"></span>
              </div>
              <p class="text-xs text-slate-400 font-mono flex items-center gap-2 mt-0.5">
                <span>更新至 {{ data?.updated_at || '-' }}</span>
                <button 
                  @click="fetchData" 
                  :disabled="isLoading"
                  class="text-slate-400 hover:text-blue-600 transition-colors p-0.5 rounded cursor-pointer"
                  title="立即刷新"
                >
                  <svg class="w-3.5 h-3.5" :class="{ 'animate-spin': isLoading }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
                </button>
              </p>
            </div>
          </div>

          <!-- 右侧汇总指标 -->
          <div class="flex items-center gap-4 text-xs font-mono">
            <div class="bg-slate-50 px-3 py-1.5 rounded-lg border border-slate-100 flex items-center gap-2">
              <span class="text-slate-400">集群可用率:</span>
              <span class="font-bold text-emerald-600">{{ data?.overall_sla !== undefined && data?.overall_sla !== null ? `${data.overall_sla}%` : '-' }}</span>
            </div>
            <div class="bg-slate-50 px-3 py-1.5 rounded-lg border border-slate-100 flex items-center gap-2">
              <span class="text-slate-400">平均首字延迟:</span>
              <span class="font-bold text-slate-700">{{ data?.overall_avg_ttft ? `${data.overall_avg_ttft}ms` : '-' }}</span>
            </div>
          </div>
        </div>

        <!-- 过滤标签行 (90m / 24h / 7d / 30d 对齐参考图) -->
        <div class="flex items-center justify-between pt-3 text-xs">
          <div class="flex items-center space-x-1 bg-slate-100/80 p-0.5 rounded-lg font-mono">
            <button
              v-for="range in (['90m', '24h', '7d', '30d'] as const)"
              :key="range"
              @click="changeRange(range)"
              class="px-2.5 py-1 rounded-md text-[11px] font-medium transition-all"
              :class="activeRange === range ? 'bg-white text-slate-800 shadow-xs font-bold' : 'text-slate-500 hover:text-slate-800'"
            >
              {{ range }}
            </button>
          </div>

          <div class="text-[11px] text-slate-400 font-mono">
            被动用量 · 真实全链路度量 · 自动容灾热切换就绪
          </div>
        </div>
      </header>

      <!-- 2. 分组展示大模型渠道流 (KEDAYA, SILICONFLOW, INFRA 等) -->
      <div v-if="isLoading && !data" class="text-center py-20 text-xs text-slate-400">
        正在拉取多模型渠道状态...
      </div>

      <div v-else class="space-y-6">
        <section 
          v-for="group in data?.groups" 
          :key="group.vendor_id"
          class="space-y-3"
        >
          <!-- 厂商分组标题 -->
          <div class="flex items-center justify-between">
            <h2 class="text-xs font-bold text-slate-400 tracking-wider uppercase flex items-center gap-2">
              <span>{{ group.vendor_name }}</span>
            </h2>
            <span class="text-[11px] text-slate-400 font-mono">{{ group.channels.length }}</span>
          </div>

          <!-- 该厂商下的模型卡片网格 -->
          <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
            <ChannelCard
              v-for="channel in group.channels"
              :key="channel.name"
              :channel="channel"
              :countdown-text="countdownText"
              @select="handleSelectChannel"
            />
          </div>
        </section>
      </div>

    </div>

    <!-- 3. 点击卡片滑出的历史审计抽屉 -->
    <ChannelAuditDrawer
      :is-open="isDrawerOpen"
      :channel="selectedChannel"
      @close="isDrawerOpen = false"
      @refresh="fetchData"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { systemApi, type ChannelsStatusResponse, type ModelChannelItem } from '../api/system';
import ChannelCard from '../components/system/ChannelCard.vue';
import ChannelAuditDrawer from '../components/system/ChannelAuditDrawer.vue';

const data = ref<ChannelsStatusResponse | null>(null);
const isLoading = ref(false);
const activeRange = ref<'90m' | '24h' | '7d' | '30d'>('90m');

const selectedChannel = ref<ModelChannelItem | null>(null);
const isDrawerOpen = ref(false);

// 30 秒轮询倒计时
const remainingSeconds = ref(30);
const countdownText = ref('30S 后刷新');
let timer: any = null;

const overallStatusDotClass = computed(() => {
  if (!data.value) return 'bg-slate-300';
  for (const g of data.value.groups) {
    for (const c of g.channels) {
      if (c.role === 'PRIMARY' && c.state === 'OPEN') return 'bg-rose-500';
      if (c.state === 'HALF_OPEN') return 'bg-amber-400';
    }
  }
  if (data.value.overall_sla < 85.0) return 'bg-rose-500';
  if (data.value.overall_sla < 95.0) return 'bg-amber-400';
  return 'bg-emerald-500';
});

const overallStatusText = computed(() => {
  if (!data.value) return '正在检测集群渠道状态...';
  if (overallStatusDotClass.value === 'bg-rose-500') return '集群存在主节点熔断或高危错误';
  if (overallStatusDotClass.value === 'bg-amber-400') return '集群正在半开探测自愈或备用节点接管';
  return '所有渠道运行正常';
});

async function fetchData() {
  isLoading.value = true;
  try {
    const res = await systemApi.getChannelsStatus(activeRange.value);
    data.value = res;
    remainingSeconds.value = 30;

    // 若抽屉正处于开启状态，同步刷新抽屉内选中的引用对象
    if (selectedChannel.value && res.groups) {
      for (const g of res.groups) {
        const match = g.channels.find(c => c.name === selectedChannel.value?.name);
        if (match) {
          selectedChannel.value = match;
          break;
        }
      }
    }
  } catch (e) {
    console.error('拉取渠道状态失败:', e);
  } finally {
    isLoading.value = false;
  }
}

function changeRange(range: '90m' | '24h' | '7d' | '30d') {
  activeRange.value = range;
  fetchData();
}

function handleSelectChannel(channel: ModelChannelItem) {
  selectedChannel.value = channel;
  isDrawerOpen.value = true;
}

onMounted(async () => {
  await fetchData();
  timer = setInterval(() => {
    remainingSeconds.value -= 1;
    if (remainingSeconds.value <= 0) {
      remainingSeconds.value = 30;
      fetchData();
    }
    countdownText.value = `${remainingSeconds.value}S 后刷新`;
  }, 1000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>
