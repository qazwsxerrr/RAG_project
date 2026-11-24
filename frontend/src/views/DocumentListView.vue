<template>
  <div class="document-list-view h-full overflow-y-auto py-8 px-4 sm:px-8 max-w-7xl mx-auto space-y-6 animate-in fade-in duration-300">
    <!-- 顶部文档统计栏 -->
    <div class="bg-white border border-slate-200/90 rounded-xl p-4 shadow-sm flex flex-wrap items-center justify-between gap-4">
      <div class="flex flex-wrap items-center divide-x divide-slate-100 text-xs">
        <div class="px-4 first:pl-0">
          <div class="text-[11px] text-slate-400 font-medium">已入库文档</div>
          <div class="mt-1 flex items-baseline gap-1.5">
            <span class="text-xl font-bold text-slate-900 font-mono">
              {{ documentStore.loading && documentStore.documents.length === 0 ? '-' : completedDocCount }}
            </span>
            <span class="text-[11px] text-slate-500">篇</span>
          </div>
        </div>

        <div class="px-4">
          <div class="text-[11px] text-slate-400 font-medium">知识切片</div>
          <div class="mt-1 flex items-baseline gap-1.5">
            <span class="text-xl font-bold text-slate-900 font-mono">
              {{ documentStore.loading && documentStore.documents.length === 0 ? '-' : totalChunkCount }}
            </span>
            <span class="text-[11px] text-slate-500">条</span>
          </div>
        </div>

        <div class="px-4">
          <div class="text-[11px] text-slate-400 font-medium">可见知识库</div>
          <div class="mt-1 flex items-baseline gap-1.5">
            <span class="text-xl font-bold text-slate-900 font-mono">
              {{ loadingKbs && accessibleKbs.length === 0 ? '-' : accessibleKbs.length }}
            </span>
            <span class="text-[11px] text-slate-500">个</span>
          </div>
        </div>
      </div>

      <!-- 右侧操作与筛选 -->
      <div class="flex items-center space-x-2">
        <button
          @click="handleRefresh"
          class="p-1.5 text-slate-400 hover:text-slate-700 hover:bg-slate-100 rounded-lg transition-colors cursor-pointer"
          title="刷新文档列表"
        >
          <svg class="w-4 h-4" :class="{ 'animate-spin': documentStore.loading }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
        </button>

        <router-link
          v-if="authStore.hasPermission(PERMISSIONS.DOC_UPLOAD)"
          to="/documents/new"
          class="px-3.5 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 shadow-sm"
        >
          <span>+ 提交文档入库</span>
        </router-link>
      </div>
    </div>

    <!-- 过滤器 -->
    <DocumentFilters
      :knowledgeBases="accessibleKbs"
      :categoryOptions="categoryOptions"
      @change="handleFilterChange"
    />

    <div v-if="loadError" class="p-3.5 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700 flex items-center justify-between gap-3">
      <span>{{ loadError }}</span>
      <button @click="handleRefresh" class="px-2.5 py-1 rounded-lg bg-white border border-rose-200 text-rose-700 hover:bg-rose-50 font-medium cursor-pointer shrink-0">重试</button>
    </div>

    <!-- 文档表格 -->
    <DocumentTable
      :documents="paginatedDocuments"
      :canDelete="authStore.hasPermission(PERMISSIONS.DOC_DELETE)"
      :loading="documentStore.loading"
      @previewDoc="handlePreviewDoc"
      @viewChunks="openChunksModal"
      @viewProgress="goToProgress"
      @reclassifyDoc="openReclassifyModal"
      @deleteDoc="handleDeleteDoc"
    />

    <!-- 轻量分页栏 -->
    <div
      v-if="filteredDocuments.length > pageSize"
      class="flex items-center justify-between text-xs text-slate-500 px-1 pt-1"
    >
      <span>
        共 <strong class="text-slate-800">{{ filteredDocuments.length }}</strong> 篇文档 · 第 {{ currentPage }} / {{ totalPages }} 页
      </span>
      <div class="flex items-center gap-2">
        <button
          @click="currentPage = Math.max(1, currentPage - 1)"
          :disabled="currentPage <= 1"
          class="px-3 py-1.5 rounded-lg border border-slate-200 bg-white hover:bg-slate-50 text-slate-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors cursor-pointer"
        >
          上一页
        </button>
        <button
          @click="currentPage = Math.min(totalPages, currentPage + 1)"
          :disabled="currentPage >= totalPages"
          class="px-3 py-1.5 rounded-lg border border-slate-200 bg-white hover:bg-slate-50 text-slate-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors cursor-pointer"
        >
          下一页
        </button>
      </div>
    </div>

    <!-- 切片下钻抽屉模态框 -->
    <DocumentChunksModal
      :visible="chunksModalVisible"
      :docId="selectedDocId"
      :fileName="selectedDocFileName"
      @close="chunksModalVisible = false"
    />

    <!-- 单篇文档在线改密模态框 -->
    <div
      v-if="reclassifyModalVisible && reclassifyTargetDoc"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-xs p-4"
      @click.self="reclassifyModalVisible = false"
    >
      <div class="bg-white rounded-2xl shadow-xl border border-slate-200 w-full max-w-md p-6 space-y-4">
        <div class="flex items-center justify-between border-b border-slate-100 pb-3">
          <h3 class="text-sm font-bold text-slate-900">调整文档密级</h3>
          <button @click="reclassifyModalVisible = false" class="text-slate-400 hover:text-slate-700 text-sm cursor-pointer">✕</button>
        </div>

        <div class="space-y-3 text-xs">
          <div class="p-3 rounded-xl bg-slate-50 border border-slate-100 space-y-1">
            <div class="font-semibold text-slate-800 truncate" :title="reclassifyTargetDoc.file_name">{{ reclassifyTargetDoc.file_name }}</div>
            <div class="text-slate-500 flex items-center gap-2">
              <span>所属库：{{ reclassifyTargetDoc.kb_name || '-' }}</span>
              <span>当前密级：</span>
              <ClassificationBadge :level="reclassifyTargetDoc.effective_classification_level ?? reclassifyTargetDoc.classification_level" />
            </div>
          </div>

          <div class="space-y-1">
            <label class="font-semibold text-slate-700 block">目标密级</label>
            <select
              v-model.number="reclassifyForm.level"
              class="w-full p-2.5 rounded-xl border border-slate-200 bg-white text-slate-800 outline-none focus:border-blue-500"
            >
              <option :value="1">L1 全员公开</option>
              <option :value="2">L2 部门内部</option>
              <option :value="3">L3 管理层机密</option>
              <option :value="4">L4 核心绝密</option>
            </select>
            <p class="text-[11px] text-slate-400">提高密级后，低密级员工将立即无法查阅或预览该文档；降低密级需具备相应安全审批权限。</p>
          </div>

          <div class="space-y-1">
            <label class="font-semibold text-slate-700 block">变更原因（写入安全审计日志）</label>
            <input
              v-model="reclassifyForm.reason"
              type="text"
              placeholder="例如：架构评审涉及核心容量指标，升级为管理层机密"
              class="w-full p-2.5 rounded-xl border border-slate-200 bg-white text-slate-800 outline-none focus:border-blue-500"
            />
          </div>

          <div v-if="reclassifyError" class="p-2.5 rounded-lg bg-rose-50 border border-rose-200 text-rose-700">
            {{ reclassifyError }}
          </div>
        </div>

        <div class="flex items-center justify-end gap-2 pt-2 border-t border-slate-100">
          <button
            type="button"
            @click="reclassifyModalVisible = false"
            class="px-4 py-2 rounded-xl border border-slate-200 text-xs font-medium text-slate-700 hover:bg-slate-50 cursor-pointer"
          >
            取消
          </button>
          <button
            type="button"
            :disabled="reclassifySaving"
            @click="submitReclassify"
            class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold disabled:opacity-50 cursor-pointer"
          >
            {{ reclassifySaving ? '提交中...' : '确认调密' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { useDocumentStore } from '../stores/document';
import { useAuthStore, PERMISSIONS } from '../stores/auth';
import { accessControlApi } from '../api/accessControl';
import { documentsApi } from '../api/documents';
import { openDocumentPreviewWithTicket } from '../utils/secureMedia';
import type { AccessibleKnowledgeBase, DocumentItem } from '../types';
import DocumentFilters from '../components/documents/DocumentFilters.vue';
import DocumentTable from '../components/documents/DocumentTable.vue';
import DocumentChunksModal from '../components/documents/DocumentChunksModal.vue';
import ClassificationBadge from '../components/common/ClassificationBadge.vue';

const router = useRouter();
const documentStore = useDocumentStore();
const authStore = useAuthStore();

const accessibleKbs = ref<AccessibleKnowledgeBase[]>([]);
const loadingKbs = ref(false);
const loadError = ref('');

const activeFilters = ref({
  keyword: '',
  kbId: '',
  classificationLevel: '',
  category: '',
  status: '',
});

const pageSize = 15;
const currentPage = ref(1);

// 切片弹窗
const chunksModalVisible = ref(false);
const selectedDocId = ref('');
const selectedDocFileName = ref('');

// 调密弹窗
const reclassifyModalVisible = ref(false);
const reclassifyTargetDoc = ref<DocumentItem | null>(null);
const reclassifySaving = ref(false);
const reclassifyError = ref('');
const reclassifyForm = reactive({
  level: 2,
  reason: '',
});

const categoryOptions = computed(() => {
  const cats = new Set<string>();
  documentStore.documents.forEach((d: DocumentItem) => {
    if (d.category) cats.add(d.category);
  });
  return Array.from(cats);
});

const completedDocCount = computed(() => {
  return documentStore.documents.filter((d: DocumentItem) => {
    const s = (d.status || '').toLowerCase();
    return s === 'active' || s === 'completed';
  }).length;
});

const totalChunkCount = computed(() => {
  return documentStore.documents.reduce((acc, doc: DocumentItem) => acc + (doc.chunk_count ?? 0), 0);
});

const filteredDocuments = computed(() => {
  return documentStore.documents.filter((doc: DocumentItem) => {
    if (activeFilters.value.keyword) {
      const kw = activeFilters.value.keyword.toLowerCase();
      const matchName = doc.file_name.toLowerCase().includes(kw);
      const matchKb = (doc.kb_name || '').toLowerCase().includes(kw);
      const matchTags = doc.tags?.some((t: string) => t.toLowerCase().includes(kw));
      if (!matchName && !matchKb && !matchTags) return false;
    }
    if (activeFilters.value.kbId && doc.kb_id !== activeFilters.value.kbId) {
      return false;
    }
    if (activeFilters.value.classificationLevel) {
      const lvl = doc.effective_classification_level ?? doc.classification_level;
      if (String(lvl) !== activeFilters.value.classificationLevel) return false;
    }
    if (activeFilters.value.category && doc.category !== activeFilters.value.category) {
      return false;
    }
    if (activeFilters.value.status) {
      const s = (doc.status || '').toLowerCase();
      const target = activeFilters.value.status.toLowerCase();
      if (target === 'queued' && s !== 'queued') return false;
      if (target === 'completed' && !(s === 'completed' || s === 'active')) return false;
      if (target === 'running' && !(s === 'running' || s === 'process')) return false;
      if (target === 'pending_review' && s !== 'pending_review') return false;
      if (target === 'failed' && s !== 'failed') return false;
    }
    return true;
  });
});

const totalPages = computed(() => Math.max(1, Math.ceil(filteredDocuments.value.length / pageSize)));

const paginatedDocuments = computed(() => {
  const start = (currentPage.value - 1) * pageSize;
  return filteredDocuments.value.slice(start, start + pageSize);
});

watch(totalPages, (tp) => {
  if (currentPage.value > tp) {
    currentPage.value = tp;
  }
});

const handleFilterChange = (filters: typeof activeFilters.value) => {
  activeFilters.value = filters;
  currentPage.value = 1;
};

const handlePreviewDoc = async (doc: DocumentItem) => {
  await openDocumentPreviewWithTicket(doc.doc_id);
};

const loadAccessibleKbs = async () => {
  loadingKbs.value = true;
  try {
    accessibleKbs.value = await accessControlApi.listAccessibleKnowledgeBases('READ');
  } catch (e: any) {
    console.warn('获取可见知识库目录失败:', e);
    loadError.value = e?.message || '获取可见知识库目录失败';
  } finally {
    loadingKbs.value = false;
  }
};

const loadDocumentsSafe = async () => {
  loadError.value = '';
  try {
    await documentStore.fetchDocuments();
  } catch (e: any) {
    loadError.value = e?.message || '加载文档列表失败，请点击重试';
  }
};

const handleRefresh = async () => {
  await Promise.all([loadDocumentsSafe(), loadAccessibleKbs()]);
};

const openChunksModal = (doc: DocumentItem) => {
  selectedDocId.value = doc.doc_id;
  selectedDocFileName.value = doc.file_name;
  chunksModalVisible.value = true;
};

const openReclassifyModal = (doc: DocumentItem) => {
  reclassifyTargetDoc.value = doc;
  reclassifyForm.level = doc.classification_level ?? 2;
  reclassifyForm.reason = '';
  reclassifyError.value = '';
  reclassifyModalVisible.value = true;
};

const submitReclassify = async () => {
  if (!reclassifyTargetDoc.value) return;
  reclassifySaving.value = true;
  reclassifyError.value = '';
  try {
    await documentsApi.updateDocumentClassification(
      reclassifyTargetDoc.value.doc_id,
      reclassifyForm.level,
      reclassifyForm.reason.trim() || undefined
    );
    reclassifyModalVisible.value = false;
    await documentStore.fetchDocuments();
  } catch (err: any) {
    reclassifyError.value = err.message || '调整密级失败';
  } finally {
    reclassifySaving.value = false;
  }
};

const goToProgress = (doc: DocumentItem) => {
  router.push({ path: '/documents/new', query: { doc_id: doc.doc_id } });
};

const handleDeleteDoc = async (doc: DocumentItem) => {
  if (confirm(`确定要永久删除文档《${doc.file_name}》吗？删除后无法恢复。`)) {
    try {
      await documentStore.deleteDoc(doc.doc_id);
    } catch (err: any) {
      alert(`删除失败: ${err.message}`);
    }
  }
};

onMounted(() => {
  void handleRefresh();
});
</script>
