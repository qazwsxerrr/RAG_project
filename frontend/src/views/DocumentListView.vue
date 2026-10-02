<template>
  <div class="document-list-view h-full overflow-y-auto py-8 px-4 sm:px-8 max-w-7xl mx-auto space-y-6 animate-in fade-in duration-300">
    <!-- 顶部资产统计大盘看板 (100% 对齐参考图单卡片多指标条结构，彻底去除冗余独立大框) -->
    <div class="bg-white border border-slate-200/90 rounded-xl p-4 shadow-xs flex flex-wrap items-center justify-between gap-4">
      <div class="flex flex-wrap items-center divide-x divide-slate-100 text-xs">
        <div class="px-4 first:pl-0">
          <div class="text-[11px] text-slate-400 font-medium">收录文档总数</div>
          <div class="mt-1 flex items-baseline gap-1.5">
            <span class="text-xl font-bold text-slate-900 font-mono">{{ documentStore.documents.length }}</span>
            <span class="text-[11px] text-slate-500">篇 (已入库)</span>
          </div>
        </div>

        <div class="px-4">
          <div class="text-[11px] text-slate-400 font-medium">知识切片总数</div>
          <div class="mt-1 flex items-baseline gap-1.5">
            <span class="text-xl font-bold text-slate-900 font-mono">89</span>
            <span class="text-[11px] text-slate-500">条切片</span>
          </div>
        </div>

        <div class="px-4">
          <div class="text-[11px] text-slate-400 font-medium">覆盖组织部门</div>
          <div class="mt-1 flex items-baseline gap-1.5">
            <span class="text-xl font-bold text-slate-900 font-mono">{{ departmentCount }}</span>
            <span class="text-[11px] text-slate-500">个部门</span>
          </div>
        </div>

        <div class="px-4">
          <div class="text-[11px] text-slate-400 font-medium">多模态高精切分</div>
          <div class="mt-1 flex items-baseline gap-1.5">
            <span class="text-sm font-bold text-slate-800 font-mono">MinerU VLM</span>
            <span class="text-[11px] text-emerald-600 font-medium">● 版面还原</span>
          </div>
        </div>

        <div class="px-4">
          <div class="text-[11px] text-slate-400 font-medium">混合向量索引</div>
          <div class="mt-1 flex items-baseline gap-1.5">
            <span class="text-sm font-bold text-emerald-600 font-mono">HNSW + BM25</span>
          </div>
        </div>
      </div>

      <!-- 右侧操作与筛选 -->
      <div class="flex items-center space-x-2">
        <button 
          @click="handleRefresh" 
          class="p-1.5 text-slate-400 hover:text-slate-700 hover:bg-slate-100 rounded-lg transition-colors cursor-pointer"
          title="刷新文档资产"
        >
          <svg class="w-4 h-4" :class="{ 'animate-spin': documentStore.loading }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
        </button>

        <router-link 
          to="/documents/new" 
          class="px-3.5 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 shadow-xs"
        >
          <span>+ 提交文档入库</span>
        </router-link>
      </div>
    </div>

    <!-- 过滤器 -->
    <DocumentFilters 
      :departmentOptions="departmentOptions"
      :categoryOptions="categoryOptions"
      @change="handleFilterChange"
    />

    <!-- 文档表格 -->
    <DocumentTable 
      :documents="filteredDocuments"
      @viewChunks="openChunksModal"
      @viewProgress="goToProgress"
      @deleteDoc="handleDeleteDoc"
    />

    <!-- 切片下钻抽屉模态框 -->
    <DocumentChunksModal 
      :visible="chunksModalVisible"
      :docId="selectedDocId"
      :fileName="selectedDocFileName"
      @close="chunksModalVisible = false"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { useDocumentStore } from '../stores/document';
import type { DocumentItem } from '../types';
import DocumentFilters from '../components/documents/DocumentFilters.vue';
import DocumentTable from '../components/documents/DocumentTable.vue';
import DocumentChunksModal from '../components/documents/DocumentChunksModal.vue';

const router = useRouter();
const documentStore = useDocumentStore();

const activeFilters = ref({
  keyword: '',
  department: '',
  category: '',
  status: '',
});

// 切片弹窗
const chunksModalVisible = ref(false);
const selectedDocId = ref('');
const selectedDocFileName = ref('');

const departmentOptions = computed(() => {
  const depts = new Set<string>();
  documentStore.documents.forEach((d: DocumentItem) => {
    if (d.department) depts.add(d.department);
  });
  return Array.from(depts);
});

const categoryOptions = computed(() => {
  const cats = new Set<string>();
  documentStore.documents.forEach((d: DocumentItem) => {
    if (d.category) cats.add(d.category);
  });
  return Array.from(cats);
});

const departmentCount = computed(() => {
  return departmentOptions.value.length || 1;
});

const filteredDocuments = computed(() => {
  return documentStore.documents.filter((doc: DocumentItem) => {
    if (activeFilters.value.keyword) {
      const kw = activeFilters.value.keyword.toLowerCase();
      const matchName = doc.file_name.toLowerCase().includes(kw);
      const matchTags = doc.tags?.some((t: string) => t.toLowerCase().includes(kw));
      if (!matchName && !matchTags) return false;
    }
    if (activeFilters.value.department && doc.department !== activeFilters.value.department) {
      return false;
    }
    if (activeFilters.value.category && doc.category !== activeFilters.value.category) {
      return false;
    }
    if (activeFilters.value.status) {
      const s = doc.status.toLowerCase();
      const target = activeFilters.value.status.toLowerCase();
      if (target === 'completed' && !(s === 'completed' || s === 'active')) return false;
      if (target === 'running' && !(s === 'running' || s === 'process')) return false;
      if (target === 'pending_review' && s !== 'pending_review') return false;
      if (target === 'failed' && s !== 'failed') return false;
    }
    return true;
  });
});

const handleFilterChange = (filters: any) => {
  activeFilters.value = filters;
};

const handleRefresh = async () => {
  await documentStore.fetchDocuments();
};

const openChunksModal = (doc: DocumentItem) => {
  selectedDocId.value = doc.doc_id;
  selectedDocFileName.value = doc.file_name;
  chunksModalVisible.value = true;
};

const goToProgress = (doc: DocumentItem) => {
  router.push({ path: '/documents/new', query: { doc_id: doc.doc_id } });
};

const handleDeleteDoc = async (doc: DocumentItem) => {
  if (confirm(`确定要永久删除文档《${doc.file_name}》及其全部向量切片吗？此操作不可逆。`)) {
    try {
      await documentStore.deleteDoc(doc.doc_id);
    } catch (err: any) {
      alert(`删除失败: ${err.message}`);
    }
  }
};

onMounted(() => {
  documentStore.fetchDocuments();
  documentStore.fetchDepartments();
});
</script>
