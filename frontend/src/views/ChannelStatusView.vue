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
                <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" title="运行正常"></span>
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
              <span class="font-bold text-emerald-600">{{ data?.overall_sla || 100 }}%</span>
            </div>
            <div class="bg-slate-50 px-3 py-1.5 rounded-lg border border-slate-100 flex items-center gap-2">
              <span class="text-slate-400">平均首字延迟:</span>
              <span class="font-bold text-slate-700">{{ data?.overall_avg_ttft || 420 }}ms</span>
            </div>
          </div>
        </div>

        <!-- 过滤标签行 (90m / 24h / 7d / 30d 对齐参考图) -->
        <div class="flex items-center justify-between pt-3 text-xs">
          <div class="flex items-center space-x-1 bg-slate-100/80 p-0.5 rounded-lg font-mono">
            <button
              v-for="range in ['90m', '24h', '7d', '30d']"
              :key="range"
              @click="activeRange = range"
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

      <!-- 2. 分组展示大模型渠道流 (KEDAYA, SILICONFLOW, INFRA) -->
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
import { ref, onMounted, onUnmounted } from 'vue';
import { systemApi, type ChannelsStatusResponse, type ModelChannelItem } from '../api/system';
import ChannelCard from '../components/system/ChannelCard.vue';
import ChannelAuditDrawer from '../components/system/ChannelAuditDrawer.vue';

const data = ref<ChannelsStatusResponse | null>(null);
const isLoading = ref(false);
const activeRange = ref('90m');

const selectedChannel = ref<ModelChannelItem | null>(null);
const isDrawerOpen = ref(false);

// 30 秒轮询倒计时
const remainingSeconds = ref(30);
const countdownText = ref('30S 后刷新');
let timer: any = null;

async function fetchData() {
  isLoading.value = true;
  try {
    const res = await systemApi.getChannelsStatus();
    data.value = res;
    remainingSeconds.value = 30;
  } catch (e) {
    console.error('拉取渠道状态失败:', e);
  } finally {
    isLoading.value = false;
  }
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
