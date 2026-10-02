import { createRouter, createWebHistory } from 'vue-router';
import DocumentListView from '../views/DocumentListView.vue';
import DocumentUploadView from '../views/DocumentUploadView.vue';
import RetrievalChatView from '../views/RetrievalChatView.vue';

const routes = [
  {
    path: '/',
    redirect: '/documents',
  },
  {
    path: '/documents',
    name: 'DocumentList',
    component: DocumentListView,
    meta: { title: '文档知识库资产' },
  },
  {
    path: '/documents/new',
    name: 'DocumentUpload',
    component: DocumentUploadView,
    meta: { title: '提交文档入库' },
  },
  {
    path: '/retrieval',
    name: 'RetrievalChat',
    component: RetrievalChatView,
    meta: { title: '智能问答检索' },
  },
  {
    path: '/chat',
    redirect: '/retrieval',
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach((to, from, next) => {
  if (to.meta.title) {
    document.title = `${to.meta.title} - 企业级多模态 RAG 智能知识工作台`;
  }
  next();
});
