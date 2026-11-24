import { defineStore } from 'pinia';
import { ref } from 'vue';
import type { DocumentItem } from '../types';
import { documentsApi } from '../api/documents';

export const useDocumentStore = defineStore('document', () => {
  const documents = ref<DocumentItem[]>([]);
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

  /** 删除指定文档 */
  const deleteDoc = async (docId: string) => {
    await documentsApi.deleteDocument(docId);
    documents.value = documents.value.filter((d) => d.doc_id !== docId);
  };

  return {
    documents,
    loading,
    error,
    fetchDocuments,
    deleteDoc,
  };
});
