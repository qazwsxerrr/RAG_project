<template>
  <div class="document-upload-view h-full overflow-y-auto py-6 px-4 sm:px-8 max-w-7xl mx-auto space-y-5 animate-in fade-in duration-300">
    <!-- 顶部状态栏与操作导航 (对齐参考图顶栏结构) -->
    <div class="flex flex-wrap items-center justify-between gap-3 pb-2 border-b border-slate-200/80">
      <div class="flex items-center space-x-3">
        <!-- 返回文档大盘按钮 -->
        <router-link 
          to="/documents"
          class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-slate-200/80 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium transition-all shadow-2xs cursor-pointer"
        >
          <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"/></svg>
          <span>返回</span>
        </router-link>

        <!-- 当前文件标题 -->
        <h2 class="text-sm sm:text-base font-bold text-slate-900 tracking-tight truncate max-w-xs sm:max-w-md">
          {{ currentFileName || (currentDocId ? '文档流水线流转详情' : '提交文档入库') }}
        </h2>

        <!-- 状态胶囊徽标 -->
        <span 
          v-if="currentDocId" 
          class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700 border border-blue-200/70"
        >
          <span class="w-1.5 h-1.5 rounded-full bg-blue-600 animate-pulse"></span>
          <span>流转中</span>
        </span>

        <!-- 文档 ID 胶囊卡片 -->
        <div 
          v-if="currentDocId"
          class="hidden md:inline-flex items-center gap-1 px-2 py-0.5 rounded-lg bg-slate-100 border border-slate-200/70 text-[11px] font-mono text-slate-600"
        >
          <span>UID {{ currentDocId.slice(0, 8) }}...</span>
          <button 
            type="button" 
            @click="copyDocId"
            class="text-slate-400 hover:text-slate-700 cursor-pointer p-0.5"
            title="复制完整文档 ID"
          >
            <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          </button>
          <span v-if="copied" class="text-emerald-600 font-sans text-[10px]">已复制</span>
        </div>
      </div>

      <!-- 右侧快速操作按钮组 -->
      <div class="flex items-center space-x-2">
        <button 
          v-if="currentDocId && !showFormAgain"
          @click="showFormAgain = true"
          class="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-xs font-medium shadow-xs flex items-center gap-1.5 transition-colors cursor-pointer"
        >
          <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          <span>录入新文件</span>
        </button>

        <button 
          v-if="showFormAgain && currentDocId"
          @click="showFormAgain = false"
          class="px-3 py-1.5 bg-white hover:bg-slate-50 border border-slate-200/80 text-slate-700 rounded-xl text-xs font-medium shadow-2xs flex items-center gap-1.5 transition-colors cursor-pointer"
        >
          <span>查看流转看板 &rarr;</span>
        </button>

        <router-link 
          to="/documents"
          class="px-3 py-1.5 bg-white hover:bg-slate-50 border border-slate-200/80 text-slate-600 hover:text-slate-900 rounded-xl text-xs font-medium shadow-2xs transition-colors"
        >
          文档列表
        </router-link>
      </div>
    </div>

    <!-- 上传表单卡片 (当未选择文档或用户点击录入新文件时显示) -->
    <DocumentForm 
      v-if="!currentDocId || showFormAgain"
      @submitted="handleSubmitted" 
    />

    <!-- 关键流转节点监控看板与实时控制台 -->
    <div v-if="currentDocId && !showFormAgain" class="space-y-5">
      <PipelineSteps 
        ref="pipelineStepsRef"
        :docId="currentDocId"
        :fileName="currentFileName"
        :autoConnect="true"
        @reviewRequired="handleReviewRequired"
        @duplicateWarning="handleDuplicateWarning"
        @completed="handleCompleted"
        @failed="handleFailed"
      />
    </div>

    <!-- 第 7 步表格人工审核模态框 (对应 Img 07) -->
    <ReviewDialog 
      :visible="reviewModalVisible"
      :docId="currentDocId"
      :items="pendingReviews"
      :initialIndex="reviewInitialIndex"
      @confirm="handleReviewConfirm"
      @close="reviewModalVisible = false"
    />

    <!-- 第 11 步防重冲突预警决策模态框 -->
    <DuplicateDialog 
      :visible="duplicateModalVisible"
      :docId="currentDocId"
      :fileName="currentFileName"
      :matchedDocId="matchedDocId"
      @resolved="handleDuplicateResolved"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRoute } from 'vue-router';
import DocumentForm from '../components/documents/DocumentForm.vue';
import PipelineSteps from '../components/documents/PipelineSteps.vue';
import ReviewDialog from '../components/documents/ReviewDialog.vue';
import DuplicateDialog from '../components/documents/DuplicateDialog.vue';
import type { ReviewItem } from '../types';
import { documentsApi } from '../api/documents';

const route = useRoute();
const pipelineStepsRef = ref<any>(null);

const currentDocId = ref<string>('');
const currentFileName = ref<string>('');
const showFormAgain = ref<boolean>(false);
const copied = ref<boolean>(false);

// 审核模态框状态
const reviewModalVisible = ref(false);
const pendingReviews = ref<ReviewItem[]>([]);
const reviewInitialIndex = ref<number>(0);

// 防重模态框状态
const duplicateModalVisible = ref(false);
const matchedDocId = ref<string | undefined>(undefined);

const copyDocId = () => {
  if (!currentDocId.value) return;
  navigator.clipboard.writeText(currentDocId.value);
  copied.value = true;
  setTimeout(() => {
    copied.value = false;
  }, 2000);
};

const handleSubmitted = (docId: string, fileName: string) => {
  currentDocId.value = docId;
  currentFileName.value = fileName;
  showFormAgain.value = false;
};

const handleReviewRequired = (items: ReviewItem[], initialIndex: number = 0) => {
  pendingReviews.value = items;
  reviewInitialIndex.value = initialIndex || 0;
  reviewModalVisible.value = true;
};

const handleReviewConfirm = async (itemsPayload: any[]) => {
  try {
    await documentsApi.submitReview(currentDocId.value, itemsPayload);
    reviewModalVisible.value = false;
    // 刷新看板
    pipelineStepsRef.value?.reconnect();
  } catch (err: any) {
    alert(`审核提交失败: ${err.message}`);
  }
};

const handleDuplicateWarning = (oldId?: string) => {
  matchedDocId.value = oldId;
  duplicateModalVisible.value = true;
};

const handleDuplicateResolved = (action: string) => {
  duplicateModalVisible.value = false;
  if (action !== 'cancel') {
    pipelineStepsRef.value?.reconnect();
  }
};

const handleCompleted = () => {
  console.log('[入库完成] docId:', currentDocId.value);
};

const handleFailed = (err: string) => {
  console.warn('[流水线执行异常] docId:', currentDocId.value, err);
};

onMounted(async () => {
  // 支持从 URL Query 参数还原文档监控: /documents/new?doc_id=xxx
  const qDocId = route.query.doc_id as string;
  if (qDocId) {
    currentDocId.value = qDocId;
    try {
      const snap = await documentsApi.getPipelineStatus(qDocId);
      if (snap && snap.file_name) {
        currentFileName.value = snap.file_name;
      }
      if (snap && (snap.overall_status === 'duplicate_warning' || snap.is_duplicate_warning)) {
        matchedDocId.value = snap.matched_doc_id;
        duplicateModalVisible.value = true;
      }
    } catch (e) {
      console.warn('获取文档快照标题失败:', e);
    }
  }
});
</script>
