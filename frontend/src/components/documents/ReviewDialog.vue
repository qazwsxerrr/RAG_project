<template>
  <Teleport to="body">
    <div v-if="visible" class="fixed inset-y-0 right-0 left-0 md:left-60 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-xs p-4 sm:p-6 animate-in fade-in duration-200" @click.self="$emit('close')">
      <div class="bg-white border border-slate-200 rounded-2xl shadow-xl max-w-4xl w-full p-6 overflow-hidden flex flex-col max-h-[90vh] text-slate-800" @click.stop>
      
      <!-- 弹窗顶栏 -->
      <div class="flex items-center justify-between pb-4 border-b border-slate-100">
        <div class="flex items-center space-x-3">
          <div class="w-8 h-8 rounded-xl bg-amber-50 border border-amber-200 flex items-center justify-center text-amber-600">
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
          </div>
          <div>
            <h3 class="text-base font-bold text-slate-900 flex items-center gap-2">
              {{ 
                currentItem?.type === 'image' ? '配图描述人工审核' : 
                currentItem?.type === 'table' ? '表格描述人工审核' : 
                currentItem?.type === 'flowchart' ? '流程图/架构图语义人工审核' : 
                currentItem?.type === 'code' ? '代码块语义人工审核' : '多模态资产人工审核'
              }}
              <span class="text-xs font-normal text-slate-500">（共 {{ items.length }} 项待审核{{ items.length > 0 ? `，当前第 ${currentIndex + 1} 项` : '' }}）</span>
            </h3>
            <p class="text-xs text-amber-700 mt-0.5">
              核对图表/代码结构与语义描述：可直接修改描述或填写备注。确认后流水线将唤醒继续入库。
            </p>
          </div>
        </div>
        <button @click="handleCancel" class="text-slate-400 hover:text-slate-700 transition-colors text-lg">&times;</button>
      </div>

      <!-- 审核内容主体 -->
      <div v-if="currentItem" class="overflow-y-auto py-4 space-y-4 flex-1 pr-1">
        <div class="flex items-center justify-between text-xs text-slate-500 font-mono bg-slate-50 px-3.5 py-2 rounded-xl border border-slate-200">
          <span>项 ID: {{ currentItem.item_id }}</span>
          <span>位置: 第 {{ (currentItem.display_page || currentItem.page_idx + 1) }} 页 · {{ currentItem.location_info || currentItem.title || '图元资产' }} {{ currentItem.line_number ? `(行号: ${currentItem.line_number})` : '' }}</span>
        </div>

        <!-- 原始图元/表格/代码/流程图渲染预览 -->
        <div class="space-y-1.5">
          <label class="text-xs font-medium text-slate-700 flex items-center gap-1">
            <svg class="w-3.5 h-3.5 text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/></svg>
            {{ 
              currentItem.type === 'image' ? '原始配图预览：' : 
              currentItem.type === 'table' ? '原始表格/切图预览：' : 
              currentItem.type === 'flowchart' ? '原始流程图/架构图预览：' : 
              currentItem.type === 'code' ? `原始代码块 (${currentItem.language || 'code'}) 预览：` : '图元预览：'
            }}
          </label>
          <!-- 表格 HTML 预览 -->
          <div 
            v-if="currentItem.type === 'table' && (currentItem.html_table || currentItem.raw_content?.includes('<table'))" 
            class="border border-slate-200 rounded-xl overflow-x-auto p-4 bg-slate-50/50 max-h-64 text-xs text-slate-800 markdown-body"
            v-html="currentItem.html_table || currentItem.raw_content"
          ></div>
          <!-- 代码块预览 (深色代码卡片) -->
          <div 
            v-else-if="currentItem.type === 'code'" 
            class="border border-slate-700 rounded-xl overflow-hidden bg-slate-900 text-xs shadow-inner"
          >
            <div class="flex items-center justify-between px-3.5 py-1.5 bg-slate-800 border-b border-slate-700 text-[11px] font-mono text-slate-400">
              <span class="flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                <span>语言: {{ currentItem.language || 'text' }}</span>
              </span>
              <span>AI 大模型语义提炼</span>
            </div>
            <pre class="p-3.5 overflow-x-auto max-h-56 font-mono text-emerald-300 leading-relaxed whitespace-pre selection:bg-emerald-900">{{ currentItem.raw_content }}</pre>
          </div>
          <!-- Mermaid 文本流程图预览 -->
          <div 
            v-else-if="currentItem.type === 'flowchart' && currentItem.raw_content && (!currentItem.image_url && !currentItem.asset_url)" 
            class="border border-slate-200 rounded-xl overflow-hidden bg-slate-50 text-xs"
          >
            <div class="flex items-center justify-between px-3.5 py-1.5 bg-slate-100 border-b border-slate-200 text-[11px] text-slate-500 font-sans">
              <span class="font-medium text-slate-700">Mermaid 流程图结构</span>
              <span>AI 大模型语义提炼</span>
            </div>
            <pre class="p-3.5 overflow-x-auto max-h-56 font-mono text-slate-700 leading-relaxed whitespace-pre text-[11px]">{{ currentItem.raw_content }}</pre>
          </div>
          <!-- 图片或切图预览 -->
          <div v-else-if="currentItem.asset_proxy_url || currentItem.image_url || currentItem.asset_url" class="border border-slate-200 rounded-xl overflow-hidden bg-slate-50 flex flex-col items-center justify-center p-2 min-h-48">
            <img 
              v-show="!imageLoadError"
              :src="currentItem.asset_proxy_url || currentItem.image_url || currentItem.asset_url" 
              :alt="currentItem.title || '审核资产截图'" 
              class="max-h-56 object-contain rounded shadow-xs"
              @error="imageLoadError = true"
              @load="imageLoadError = false"
            />
            <div v-if="imageLoadError" class="text-xs text-amber-600 flex items-center gap-1.5 py-8">
              <svg class="w-4 h-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
              <span>图片加载失败，请检查服务连接</span>
            </div>
          </div>
          <div v-else class="text-xs text-slate-400 italic p-3 bg-slate-50 rounded-lg">
            暂无原生图元或 HTML 结构，依据提取文本进行复核。
          </div>
        </div>

        <!-- 语义描述编辑 -->
        <div class="space-y-1.5">
          <div class="flex items-center justify-between">
            <label class="text-xs font-medium text-slate-700 flex items-center gap-1">
              <svg class="w-3.5 h-3.5 text-emerald-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
              AI 大模型语义提炼描述 (可在线编辑)：
            </label>
            <span class="text-[11px] text-slate-500">已编辑 {{ editableDescriptions[currentItem.item_id]?.length || 0 }} 字</span>
          </div>
          <textarea 
            v-model="editableDescriptions[currentItem.item_id]"
            rows="5"
            class="w-full text-xs font-mono p-3 rounded-xl border border-slate-200 bg-white text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 outline-none transition-all resize-y shadow-sm"
            :placeholder="
              currentItem.type === 'code' ? '在此编辑校正代码块的功能与核心逻辑语义描述...' :
              currentItem.type === 'flowchart' ? '在此编辑校正流程图/架构图的流转与节点语义描述...' :
              currentItem.type === 'table' ? '在此编辑校正表格的标准纯文本语义序列化描述...' :
              '在此编辑校正配图语义描述...'
            "
          ></textarea>
        </div>

        <!-- 备注信息输入 -->
        <div class="space-y-1.5">
          <label class="text-xs font-medium text-slate-600">审核备注 (可选)：</label>
          <input 
            v-model="remarks[currentItem.item_id]"
            type="text"
            placeholder="填写人工核对意见，如：核对无误、修改了第二行数值等"
            class="w-full text-xs p-2.5 rounded-xl border border-slate-200 bg-white text-slate-800 placeholder-slate-400 focus:border-blue-500 outline-none shadow-sm"
          />
        </div>
      </div>

      <!-- 待审核项为空时的防空白占位提示 -->
      <div v-else class="flex flex-col items-center justify-center py-16 text-slate-400 space-y-3 flex-1">
        <svg class="w-12 h-12 text-slate-300 animate-pulse" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
        </svg>
        <div class="text-center">
          <p class="text-sm font-medium text-slate-600">正在同步审核数据或无待核对项</p>
          <p class="text-xs text-slate-400 mt-1">若长时间无响应，可点击流水线右上角“重新连接”同步最新快照</p>
        </div>
      </div>

      <!-- 弹窗底栏操作 -->
      <div class="pt-4 border-t border-slate-100 flex items-center justify-between">
        <div class="flex items-center space-x-2">
          <button 
            v-if="currentIndex > 0"
            @click="currentIndex--"
            class="px-3 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs rounded-lg transition-colors"
          >
            上一项
          </button>
          <button 
            v-if="currentIndex < items.length - 1"
            @click="currentIndex++"
            class="px-3 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs rounded-lg transition-colors"
          >
            下一项
          </button>
        </div>

        <div class="flex items-center space-x-3">
          <button 
            @click="handleCancel" 
            class="px-4 py-2 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-medium rounded-xl transition-all"
          >
            稍后审核
          </button>
          <button 
            @click="handleConfirm" 
            :disabled="!currentItem || submitting"
            class="px-5 py-2 bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold rounded-xl shadow-sm shadow-blue-500/20 transition-all flex items-center gap-1.5 disabled:opacity-50"
          >
            <svg v-if="submitting" class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
            <span>确认并继续</span>
          </button>
        </div>
      </div>

    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch, computed } from 'vue';
import type { ReviewItem } from '../../types';

const props = withDefaults(defineProps<{
  visible: boolean;
  docId: string;
  items: ReviewItem[];
  initialIndex?: number;
}>(), {
  initialIndex: 0
});

const emit = defineEmits<{
  (e: 'confirm', payload: Array<{ item_id: string; user_description: string; remark?: string; approved?: boolean }>): void;
  (e: 'close'): void;
}>();

const currentIndex = ref(0);
const editableDescriptions = ref<Record<string, string>>({});
const remarks = ref<Record<string, string>>({});
const submitting = ref(false);
const imageLoadError = ref(false);

watch(
  () => props.visible,
  (v) => {
    if (v) {
      currentIndex.value = props.initialIndex || 0;
      imageLoadError.value = false;
    }
  }
);

watch(currentIndex, () => {
  imageLoadError.value = false;
});

const currentItem = computed(() => {
  return props.items[currentIndex.value] || null;
});

// 初始化编辑内容
watch(
  () => props.items,
  (newItems) => {
    if (newItems && newItems.length > 0) {
      newItems.forEach((item) => {
        if (!editableDescriptions.value[item.item_id]) {
          editableDescriptions.value[item.item_id] = item.user_description || item.vlm_description || '';
        }
        if (!remarks.value[item.item_id]) {
          remarks.value[item.item_id] = item.remark || '';
        }
      });
      currentIndex.value = 0;
    }
  },
  { immediate: true, deep: true }
);

const handleCancel = () => {
  emit('close');
};

const handleConfirm = () => {
  submitting.value = true;
  const payload = props.items.map((item) => ({
    item_id: item.item_id,
    user_description: editableDescriptions.value[item.item_id] || item.vlm_description || '',
    remark: remarks.value[item.item_id] || '确认无误',
    approved: true,
  }));
  emit('confirm', payload);
  submitting.value = false;
};
</script>
