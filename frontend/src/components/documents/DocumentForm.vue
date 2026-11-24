<template>
  <div class="glass-panel rounded-2xl p-6 sm:p-8 shadow-sm border border-slate-200/90 bg-white text-slate-800 max-w-4xl mx-auto">
    <div class="mb-6 pb-4 border-b border-slate-100 flex items-center justify-between gap-3">
      <h2 class="text-lg font-bold text-slate-900 tracking-tight flex items-center gap-2">
        <svg class="w-5 h-5 text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        提交文档入库
      </h2>
      <div v-if="authStore.currentUser" class="flex items-center gap-2 text-xs text-slate-500">
        <span>当前密级上限</span>
        <ClassificationBadge :level="authStore.currentUser.clearance_level" />
      </div>
    </div>

    <div
      v-if="kbLoadError"
      class="mb-6 p-4 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800 flex items-center justify-between gap-3"
    >
      <span>{{ kbLoadError }}</span>
      <button
        type="button"
        @click="emit('retryLoadKbs')"
        class="px-2.5 py-1 rounded-lg bg-white border border-rose-200 text-rose-700 hover:bg-rose-50 font-medium cursor-pointer shrink-0"
      >
        重试
      </button>
    </div>

    <div
      v-else-if="!loadingKbs && uploadableKbs.length === 0"
      class="mb-6 p-4 rounded-xl bg-amber-50 border border-amber-200 text-xs text-amber-900 space-y-1"
    >
      <div class="font-semibold">当前账号暂无可上传文档的知识库</div>
      <p class="text-amber-800">请联系部门管理员或系统管理员为您开通目标知识库的上传权限。</p>
    </div>

    <form @submit.prevent="handleSubmit" class="space-y-6">
      <!-- 拖拽上传卡片 -->
      <div
        @dragover.prevent="isDragging = true"
        @dragleave.prevent="isDragging = false"
        @drop.prevent="handleDrop"
        @click="triggerFileInput"
        class="border-2 border-dashed rounded-2xl p-8 text-center cursor-pointer transition-all duration-200 flex flex-col items-center justify-center relative overflow-hidden group"
        :class="[
          isDragging ? 'border-blue-500 bg-blue-50/50 scale-[1.01]' :
          selectedFile ? 'border-emerald-500/60 bg-emerald-50/30' :
          'border-slate-200 hover:border-blue-400 bg-slate-50/50 hover:bg-slate-50'
        ]"
      >
        <input
          ref="fileInputRef"
          type="file"
          accept=".pdf,.docx,.pptx,.md,.txt"
          class="hidden"
          @change="handleFileChange"
        />

        <div v-if="selectedFile" class="flex flex-col items-center space-y-2">
          <div class="w-12 h-12 rounded-2xl bg-emerald-100 border border-emerald-200 flex items-center justify-center text-emerald-600">
            <svg class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><polyline points="9 15 12 18 15 15"/></svg>
          </div>
          <div class="text-sm font-semibold text-slate-900">{{ selectedFile.name }}</div>
          <div class="text-xs text-slate-500 font-mono">{{ formatFileSize(selectedFile.size) }} · 点击或拖拽可更换文件</div>
        </div>

        <div v-else class="flex flex-col items-center space-y-3">
          <div class="w-12 h-12 rounded-2xl bg-blue-50 border border-blue-200 flex items-center justify-center text-blue-600 group-hover:scale-110 transition-transform">
            <svg class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          </div>
          <div>
            <span class="text-sm font-semibold text-slate-800 group-hover:text-blue-600 transition-colors">点击上传或将文件拖入此区域</span>
            <p class="text-xs text-slate-500 mt-1">支持格式：PDF、DOCX、PPTX、Markdown (.md)、TXT（单文件最大 100MB）</p>
          </div>
        </div>
      </div>

      <!-- 表单输入区域 -->
      <div class="grid grid-cols-1 md:grid-cols-2 gap-5">
        <!-- 文件名 -->
        <div class="space-y-1.5 md:col-span-2">
          <label class="text-xs font-semibold text-slate-700">文件名：</label>
          <input
            v-model="form.customFileName"
            type="text"
            placeholder="上传文件后自动填入，可手工微调"
            class="w-full text-xs p-3 rounded-xl border border-slate-200 bg-white text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 outline-none transition-all shadow-sm"
          />
        </div>

        <!-- 目标知识库选择器 -->
        <div class="space-y-1.5">
          <label class="text-xs font-semibold text-slate-700 block">目标知识库：</label>
          <select
            v-model="form.kbId"
            :disabled="loadingKbs || uploadableKbs.length === 0"
            class="w-full text-xs p-3 rounded-xl border border-slate-200 bg-white text-slate-800 focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 outline-none transition-all shadow-sm cursor-pointer disabled:bg-slate-50 disabled:text-slate-400"
          >
            <option v-if="loadingKbs" value="">正在加载可上传知识库...</option>
            <option v-else-if="uploadableKbs.length === 0" value="">暂无可上传知识库</option>
            <optgroup v-if="groupedKbs.PUBLIC.length > 0" label="全员公开库">
              <option v-for="kb in groupedKbs.PUBLIC" :key="kb.id" :value="kb.id">
                {{ kb.name }} (L{{ kb.min_classification_level }}~L{{ kb.max_doc_level }})
              </option>
            </optgroup>
            <optgroup v-if="groupedKbs.DEPARTMENT.length > 0" label="所属部门库">
              <option v-for="kb in groupedKbs.DEPARTMENT" :key="kb.id" :value="kb.id">
                {{ kb.name }} · {{ kb.owner_dept_path }} (L{{ kb.min_classification_level }}~L{{ kb.max_doc_level }})
              </option>
            </optgroup>
            <optgroup v-if="groupedKbs.SHARED_ACL.length > 0" label="跨部门共享库">
              <option v-for="kb in groupedKbs.SHARED_ACL" :key="kb.id" :value="kb.id">
                {{ kb.name }} (L{{ kb.min_classification_level }}~L{{ kb.max_doc_level }})
              </option>
            </optgroup>
          </select>
        </div>

        <!-- 文档密级选择器 -->
        <div class="space-y-1.5">
          <label class="text-xs font-semibold text-slate-700 flex items-center justify-between">
            <span>文档密级：</span>
            <span v-if="selectedKb && allowedLevelHint" class="text-[11px] text-slate-500 font-normal">
              {{ allowedLevelHint }}
            </span>
          </label>
          <select
            v-model.number="form.classificationLevel"
            :disabled="!selectedKb || allowedLevelOptions.length === 0"
            class="w-full text-xs p-3 rounded-xl border border-slate-200 bg-white text-slate-800 focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 outline-none transition-all shadow-sm cursor-pointer disabled:bg-slate-50 disabled:text-slate-400"
          >
            <option v-for="lvl in allowedLevelOptions" :key="lvl" :value="lvl">
              {{ CLASSIFICATION_META[lvl]?.label || `L${lvl}` }}
            </option>
          </select>
        </div>

        <!-- 分类输入框 -->
        <div class="space-y-1.5 md:col-span-2">
          <label class="text-xs font-semibold text-slate-700">文档分类：</label>
          <input
            v-model="form.category"
            type="text"
            placeholder="例如：技术规范 / 行业报告 / 商务合同 / 制度文件"
            class="w-full text-xs p-3 rounded-xl border border-slate-200 bg-white text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 outline-none transition-all shadow-sm"
          />
        </div>

        <!-- 多标签输入框 -->
        <div class="space-y-1.5 md:col-span-2">
          <label class="text-xs font-semibold text-slate-700 flex items-center justify-between">
            <span>多标签 (Tag)：</span>
            <span class="text-[11px] text-slate-500">输入标签内容后按回车添加</span>
          </label>

          <div class="p-2 min-h-[46px] rounded-xl border border-slate-200 bg-white flex flex-wrap items-center gap-1.5 focus-within:border-blue-500 focus-within:ring-1 focus-within:ring-blue-500/20 transition-all shadow-sm">
            <span
              v-for="(tag, idx) in form.tags"
              :key="idx"
              class="inline-flex items-center gap-1 text-xs text-slate-700 bg-slate-100 px-2 py-0.5 rounded font-mono transition-all"
            >
              <span>#{{ tag }}</span>
              <button
                type="button"
                @click="removeTag(idx)"
                class="text-slate-400 hover:text-rose-600 transition-colors ml-0.5"
                title="移除标签"
              >
                &times;
              </button>
            </span>

            <input
              v-model="currentTagInput"
              @keydown.enter.prevent="addTag"
              type="text"
              placeholder="输入标签按回车..."
              class="flex-1 min-w-[120px] bg-transparent text-xs text-slate-800 placeholder-slate-400 outline-none p-1"
            />
          </div>
        </div>

        <!-- 人工审核控制选项 -->
        <div class="md:col-span-2 flex items-center space-x-2 pt-1">
          <input
            v-model="form.skipReview"
            id="skip-review"
            type="checkbox"
            class="rounded bg-white border-slate-300 text-blue-600 focus:ring-0 focus:ring-offset-0 w-4 h-4 cursor-pointer"
          />
          <label for="skip-review" class="text-xs text-slate-600 select-none cursor-pointer">
            跳过表格与图表人工审核
          </label>
        </div>
      </div>

      <!-- 底部操作按钮 -->
      <div class="pt-4 border-t border-slate-100 flex items-center justify-end space-x-3">
        <button
          type="button"
          @click="resetForm"
          :disabled="submitting"
          class="px-5 py-2.5 rounded-xl border border-slate-200 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium transition-all disabled:opacity-50 shadow-sm cursor-pointer"
        >
          清空
        </button>
        <button
          type="submit"
          :disabled="!selectedFile || !form.kbId || allowedLevelOptions.length === 0 || submitting"
          class="px-6 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold shadow-sm shadow-blue-500/25 transition-all flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
        >
          <svg v-if="submitting" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
          <span>{{ submitting ? '正在上传文件...' : '提交入库' }}</span>
        </button>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue';
import type { AccessibleKnowledgeBase } from '../../types';
import { CLASSIFICATION_META } from '../../types';
import { documentsApi } from '../../api/documents';
import { useAuthStore } from '../../stores/auth';
import ClassificationBadge from '../common/ClassificationBadge.vue';

const props = withDefaults(
  defineProps<{
    uploadableKbs?: AccessibleKnowledgeBase[];
    loadingKbs?: boolean;
    kbLoadError?: string;
  }>(),
  {
    uploadableKbs: () => [],
    loadingKbs: false,
    kbLoadError: '',
  }
);

const emit = defineEmits<{
  (e: 'submitted', docId: string, fileName: string): void;
  (e: 'retryLoadKbs'): void;
}>();

const authStore = useAuthStore();
const fileInputRef = ref<HTMLInputElement | null>(null);
const selectedFile = ref<File | null>(null);
const isDragging = ref(false);
const submitting = ref(false);
const currentTagInput = ref('');

const uploadableKbs = computed(() => props.uploadableKbs);
const loadingKbs = computed(() => props.loadingKbs);

const groupedKbs = computed(() => {
  const groups: Record<'PUBLIC' | 'DEPARTMENT' | 'SHARED_ACL', AccessibleKnowledgeBase[]> = {
    PUBLIC: [],
    DEPARTMENT: [],
    SHARED_ACL: [],
  };
  for (const kb of uploadableKbs.value) {
    if (kb.source_type === 'PUBLIC') {
      groups.PUBLIC.push(kb);
    } else if (kb.source_type === 'DEPARTMENT') {
      groups.DEPARTMENT.push(kb);
    } else {
      groups.SHARED_ACL.push(kb);
    }
  }
  return groups;
});

const form = reactive({
  customFileName: '',
  kbId: '',
  classificationLevel: 1,
  category: '技术规范',
  tags: ['RAG', '知识库'] as string[],
  skipReview: false,
});

const selectedKb = computed(() => uploadableKbs.value.find((kb) => kb.id === form.kbId) || null);

const allowedLevelOptions = computed<number[]>(() => {
  const kb = selectedKb.value;
  if (!kb) return [];
  return Array.isArray(kb.allowed_upload_levels) ? kb.allowed_upload_levels : [];
});

const allowedLevelHint = computed(() => {
  const levels = allowedLevelOptions.value;
  if (levels.length === 0) return '';
  if (levels.length === 1) return `可选 L${levels[0]}`;
  return `可选 L${levels[0]}~L${levels[levels.length - 1]}`;
});

watch(
  selectedKb,
  (kb) => {
    if (!kb) return;
    const levels = allowedLevelOptions.value;
    if (levels.includes(kb.default_doc_level)) {
      form.classificationLevel = kb.default_doc_level;
    } else if (levels.length > 0) {
      form.classificationLevel = levels[0];
    }
  },
  { immediate: true }
);

const triggerFileInput = () => {
  fileInputRef.value?.click();
};

let lastAutoFileName = '';

const handleFileChange = (e: Event) => {
  const target = e.target as HTMLInputElement;
  if (target.files && target.files[0]) {
    setFile(target.files[0]);
  }
  target.value = '';
};

const handleDrop = (e: DragEvent) => {
  isDragging.value = false;
  if (e.dataTransfer?.files && e.dataTransfer.files[0]) {
    setFile(e.dataTransfer.files[0]);
  }
};

const setFile = (file: File) => {
  selectedFile.value = file;
  if (!form.customFileName || form.customFileName === lastAutoFileName) {
    form.customFileName = file.name;
  }
  lastAutoFileName = file.name;
};

const addTag = () => {
  const val = currentTagInput.value.trim();
  if (val && !form.tags.includes(val)) {
    form.tags.push(val);
  }
  currentTagInput.value = '';
};

const removeTag = (index: number) => {
  form.tags.splice(index, 1);
};

const formatFileSize = (bytes: number): string => {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
};

const pickDefaultKb = () => {
  if (groupedKbs.value.DEPARTMENT.length > 0) {
    form.kbId = groupedKbs.value.DEPARTMENT[0].id;
  } else if (uploadableKbs.value.length > 0) {
    form.kbId = uploadableKbs.value[0].id;
  } else {
    form.kbId = '';
  }
};

const resetForm = () => {
  selectedFile.value = null;
  lastAutoFileName = '';
  if (fileInputRef.value) {
    fileInputRef.value.value = '';
  }
  form.customFileName = '';
  pickDefaultKb();
  form.category = '技术规范';
  form.tags = ['RAG', '知识库'];
  form.skipReview = false;
  currentTagInput.value = '';
};

const handleSubmit = async () => {
  if (!selectedFile.value || !form.kbId) return;

  submitting.value = true;
  try {
    const formData = new FormData();
    formData.append('file', selectedFile.value);
    formData.append('kb_id', form.kbId);
    formData.append('classification_level', String(form.classificationLevel));
    formData.append('category', form.category.trim());
    formData.append('tags', JSON.stringify(form.tags));
    formData.append('skip_review', String(form.skipReview));
    if (form.customFileName.trim()) {
      formData.append('custom_file_name', form.customFileName.trim());
    }

    const res = await documentsApi.uploadDocument(formData);
    emit('submitted', res.doc_id, res.file_name);
  } catch (err: any) {
    alert(`上传失败: ${err.message}`);
  } finally {
    submitting.value = false;
  }
};

watch(
  uploadableKbs,
  (list) => {
    if (list.length > 0 && !list.some((kb) => kb.id === form.kbId)) {
      pickDefaultKb();
    } else if (list.length === 0) {
      form.kbId = '';
    }
  },
  { immediate: true }
);
</script>
