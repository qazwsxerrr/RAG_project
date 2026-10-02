import { defineStore } from 'pinia';
import { ref } from 'vue';
import type { DocumentItem, DepartmentItem, PipelineSnapshot } from '../types';
import { documentsApi } from '../api/documents';

export const useDocumentStore = defineStore('document', () => {
  const documents = ref<DocumentItem[]>([]);
  const departments = ref<DepartmentItem[]>([]);
  const loading = ref<boolean>(false);
  const error = ref<string | null>(null);

  /** 加载所有文档 */
  const fetchDocuments = async () => {
    loading.value = true;
    error.value = null;
    try {
      documents.value = await documentsApi.getDocuments();
    } catch (err: any) {
      error.value = err.message;
      throw err;
    } finally {
      loading.value = false;
    }
  };

  /** 加载部门列表 */
  const fetchDepartments = async () => {
    try {
      departments.value = await documentsApi.getDepartments();
    } catch (err: any) {
      console.warn('获取部门列表失败:', err);
    }
  };

  /** 删除指定文档 */
  const deleteDoc = async (docId: string) => {
    await documentsApi.deleteDocument(docId);
    documents.value = documents.value.filter((d) => d.doc_id !== docId);
  };

  return {
    documents,
    departments,
    loading,
    error,
    fetchDocuments,
    fetchDepartments,
    deleteDoc,
  };
});
