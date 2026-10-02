<template>
  <Teleport to="body">
    <div v-if="visible" class="fixed inset-y-0 right-0 left-0 md:left-60 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-xs p-4 sm:p-6 animate-in fade-in duration-200" @click.self="handleAction('cancel')">
      <div class="bg-white border border-amber-300 rounded-2xl shadow-xl max-w-lg w-full p-6 text-slate-800 overflow-hidden" @click.stop>
      
      <div class="flex items-center space-x-3 pb-3.5 border-b border-slate-100">
        <div class="w-9 h-9 rounded-xl bg-amber-50 border border-amber-200 flex items-center justify-center text-amber-600">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        </div>
        <div>
          <h3 class="text-base font-bold text-slate-900">检测到重复文档预警</h3>
          <p class="text-xs text-amber-700 mt-0.5">该文档的标题或文件内容与现有知识库高精度吻合</p>
        </div>
      </div>

      <div class="py-4 space-y-3 text-xs text-slate-600">
        <p class="bg-slate-50 p-3.5 rounded-xl border border-slate-200 leading-relaxed font-mono">
          当前待入库文档：<span class="text-blue-700 font-semibold">{{ fileName }}</span><br/>
          <span v-if="matchedDocId" class="text-slate-500">匹配到的已有文档 ID: {{ matchedDocId }}</span>
        </p>
        <p class="text-slate-500">
          企业级知识库要求严格防止冗余重复知识污染检索召回。请决策如何处理此文档：
        </p>
      </div>

      <div class="space-y-2.5 pt-1">
        <button 
          @click="handleAction('overwrite')"
          :disabled="loading"
          class="w-full flex items-center justify-between p-3.5 rounded-xl border border-blue-200 bg-blue-50/70 hover:bg-blue-50 text-left transition-all group disabled:opacity-50"
        >
          <div>
            <div class="text-xs font-semibold text-blue-900 flex items-center gap-1.5">
              <span>覆盖升级版本 (推荐)</span>
              <span class="text-[10px] bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded font-medium">版本自增</span>
            </div>
            <div class="text-[11px] text-blue-700/80 mt-0.5">将旧文档标记归档并自动下线，以新切片无缝覆盖升级</div>
          </div>
          <span class="text-blue-600 group-hover:translate-x-1 transition-transform">&rarr;</span>
        </button>

        <button 
          @click="handleAction('create_new')"
          :disabled="loading"
          class="w-full flex items-center justify-between p-3.5 rounded-xl border border-slate-200 bg-white hover:bg-slate-50 text-left transition-all group disabled:opacity-50 shadow-sm"
        >
          <div>
            <div class="text-xs font-semibold text-slate-800">作为全新独立文档入库</div>
            <div class="text-[11px] text-slate-500 mt-0.5">保留旧文档并存，作为独立 UUID 文档继续切片入库</div>
          </div>
          <span class="text-slate-500 group-hover:translate-x-1 transition-transform">&rarr;</span>
        </button>

        <button 
          @click="handleAction('cancel')"
          :disabled="loading"
          class="w-full py-2.5 text-center text-xs text-rose-600 hover:text-rose-700 hover:bg-rose-50 rounded-xl transition-all font-medium disabled:opacity-50"
        >
          取消并放弃本次入库
        </button>
      </div>

    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { documentsApi } from '../../api/documents';

const props = defineProps<{
  visible: boolean;
  docId: string;
  fileName: string;
  matchedDocId?: string;
}>();

const emit = defineEmits<{
  (e: 'resolved', action: 'overwrite' | 'create_new' | 'cancel'): void;
}>();

const loading = ref(false);

const handleAction = async (action: 'overwrite' | 'create_new' | 'cancel') => {
  loading.value = true;
  try {
    await documentsApi.resolveDuplicate(props.docId, action, props.matchedDocId);
    emit('resolved', action);
  } catch (err: any) {
    alert(`处理失败: ${err.message}`);
  } finally {
    loading.value = false;
  }
};
</script>
