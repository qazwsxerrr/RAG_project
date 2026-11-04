import { createRouter, createWebHistory } from 'vue-router';
import DocumentListView from '../views/DocumentListView.vue';
import DocumentUploadView from '../views/DocumentUploadView.vue';
import RetrievalChatView from '../views/RetrievalChatView.vue';
import ChannelStatusView from '../views/ChannelStatusView.vue';
import AuthCallbackView from '../views/AuthCallbackView.vue';
import { useAuthStore } from '../stores/auth';

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
    path: '/channels',
    name: 'ChannelStatus',
    component: ChannelStatusView,
    meta: { title: '大模型渠道状态与SLA监控' },
  },
  {
    path: '/auth/callback',
    name: 'AuthCallback',
    component: AuthCallbackView,
    meta: { title: '企业身份认证', public: true },
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

router.beforeEach(async (to, from, next) => {
  if (to.meta.title) {
    document.title = `${to.meta.title} - 企业级多模态 RAG 智能知识工作台`;
  }

  // 免守卫白名单端点 (OIDC 回调等)
  if (to.meta.public || to.path === '/auth/callback') {
    return next();
  }

  const authStore = useAuthStore();
  if (!authStore.token) {
    try {
      await authStore.initAuth();
      if (authStore.token) {
        return next();
      }
    } catch {
      // 内存无票且换票失败，跳转企业 SSO
    }
    sessionStorage.setItem('rag_auth_redirect', to.fullPath);
    window.location.href = '/oauth2/authorization/corp-sso';
    return;
  }

  next();
});
