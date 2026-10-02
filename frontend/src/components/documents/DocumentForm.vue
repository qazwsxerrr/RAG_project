<template>
  <div class="glass-panel rounded-2xl p-6 sm:p-8 shadow-sm border border-slate-200/90 bg-white text-slate-800 max-w-4xl mx-auto">
    <div class="mb-6 pb-4 border-b border-slate-100 flex items-center justify-between">
      <div>
        <h2 class="text-lg font-bold text-slate-900 tracking-tight flex items-center gap-2">
          <svg class="w-5 h-5 text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          提交文档入库
        </h2>
        <p class="text-xs text-slate-500 mt-1">支持多模态高精版面分析（MinerU）、表格/图片 VLM 语义增强与三路混合向量索引</p>
      </div>
      <span class="text-xs text-slate-400 font-mono">
        Phase 4 生产流水线
      </span>
    </div>

    <form @submit.prevent="handleSubmit" class="space-y-6">
      <!-- 拖拽上传卡片 (方案 D 浅灰雅致拖拽区) -->
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

        <!-- 已选文件提示 -->
        <div v-if="selectedFile" class="flex flex-col items-center space-y-2">
          <div class="w-12 h-12 rounded-2xl bg-emerald-100 border border-emerald-200 flex items-center justify-center text-emerald-600">
            <svg class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><polyline points="9 15 12 18 15 15"/></svg>
          </div>
          <div class="text-sm font-semibold text-slate-900">{{ selectedFile.name }}</div>
          <div class="text-xs text-slate-500 font-mono">{{ formatFileSize(selectedFile.size) }} · 点击或拖拽可更换文件</div>
        </div>

        <!-- 未选文件上传占位 -->
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
        <!-- 文件名 (自动回填，支持手工重命名) -->
        <div class="space-y-1.5 md:col-span-2">
          <label class="text-xs font-semibold text-slate-700">文件名：</label>
          <input 
            v-model="form.customFileName"
            type="text" 
            placeholder="上传文件后自动填入，可手工微调" 
            class="w-full text-xs p-3 rounded-xl border border-slate-200 bg-white text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 outline-none transition-all shadow-sm"
          />
        </div>

        <!-- 部门输入框 (支持已有下拉与直接打字新增，对应 Img 01) -->
        <div class="space-y-1.5 relative">
          <label class="text-xs font-semibold text-slate-700 flex items-center justify-between">
            <span>部门：</span>
            <span class="text-[11px] text-blue-600 font-normal">没有的可以直接输入新增</span>
          </label>
          <div class="relative">
            <input 
              v-model="form.department"
              type="text" 
              placeholder="选择部门，没有的可以直接输入新增" 
              list="dept-list-options"
              class="w-full text-xs p-3 rounded-xl border border-slate-200 bg-white text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 outline-none transition-all shadow-sm"
            />
            <datalist id="dept-list-options">
              <option v-for="dept in departmentList" :key="dept.id" :value="dept.name" />
            </datalist>
          </div>
        </div>

        <!-- 分类输入框 -->
        <div class="space-y-1.5">
          <label class="text-xs font-semibold text-slate-700">分类：</label>
          <input 
            v-model="form.category"
            type="text" 
            placeholder="例如：技术规范 / 行业报告 / 面试题 / 制度文件" 
            class="w-full text-xs p-3 rounded-xl border border-slate-200 bg-white text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 outline-none transition-all shadow-sm"
          />
        </div>

        <!-- 标签输入框 (对应 Img 03，回车添加胶囊，支持删除) -->
        <div class="space-y-1.5 md:col-span-2">
          <label class="text-xs font-semibold text-slate-700 flex items-center justify-between">
            <span>多标签 (Tag)：</span>
            <span class="text-[11px] text-slate-500">输入标签内容后按回车添加</span>
          </label>
          
          <div class="p-2 min-h-[46px] rounded-xl border border-slate-200 bg-white flex flex-wrap items-center gap-1.5 focus-within:border-blue-500 focus-within:ring-1 focus-within:ring-blue-500/20 transition-all shadow-sm">
            <!-- 标签列表 (去除蓝色外边框药丸盒) -->
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

            <!-- 输入文本框 -->
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
            跳过第 7 步表格人工审核（全自动入库，适用于后台无人值守批量导入）
          </label>
        </div>
      </div>

      <!-- 底部操作按钮 -->
      <div class="pt-4 border-t border-slate-100 flex items-center justify-end space-x-3">
        <button 
          type="button" 
          @click="resetForm" 
          :disabled="submitting"
          class="px-5 py-2.5 rounded-xl border border-slate-200 bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium transition-all disabled:opacity-50 shadow-sm"
        >
          清空
        </button>
        <button 
          type="submit" 
          :disabled="!selectedFile || submitting"
          class="px-6 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold shadow-sm shadow-blue-500/25 transition-all flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <svg v-if="submitting" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
          <span>{{ submitting ? '正在上传启动流水线...' : '提交入库' }}</span>
        </button>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue';
import type { DepartmentItem } from '../../types';
import { documentsApi } from '../../api/documents';

const emit = defineEmits<{
  (e: 'submitted', docId: string, fileName: string): void;
}>();

const fileInputRef = ref<HTMLInputElement | null>(null);
const selectedFile = ref<File | null>(null);
const isDragging = ref(false);
const submitting = ref(false);
const departmentList = ref<DepartmentItem[]>([]);
const currentTagInput = ref('');

const form = reactive({
  customFileName: '',
  department: '技术部',
  category: '技术规范',
  tags: ['RAG', '知识库'] as string[],
  skipReview: false,
});

const triggerFileInput = () => {
  fileInputRef.value?.click();
};

const handleFileChange = (e: Event) => {
  const target = e.target as HTMLInputElement;
  if (target.files && target.files[0]) {
    setFile(target.files[0]);
  }
};

const handleDrop = (e: DragEvent) => {
  isDragging.value = false;
  if (e.dataTransfer?.files && e.dataTransfer.files[0]) {
    setFile(e.dataTransfer.files[0]);
  }
};

const setFile = (file: File) => {
  selectedFile.value = file;
  if (!form.customFileName) {
    form.customFileName = file.name;
  }
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

const resetForm = () => {
  selectedFile.value = null;
  form.customFileName = '';
  form.department = '技术部';
  form.category = '技术规范';
  form.tags = ['RAG', '知识库'];
  form.skipReview = false;
  currentTagInput.value = '';
};

const handleSubmit = async () => {
  if (!selectedFile.value) return;

  submitting.value = true;
  try {
    const formData = new FormData();
    formData.append('file', selectedFile.value);
    formData.append('department', form.department.trim());
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

onMounted(async () => {
  try {
    departmentList.value = await documentsApi.getDepartments();
  } catch (e) {
    console.warn('初始化部门列表失败:', e);
  }
});
</script>
