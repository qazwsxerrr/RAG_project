import { createRouter, createWebHistory } from 'vue-router';
import { useAuthStore } from '../stores/auth';

const routes = [
  {
    path: '/',
    redirect: '/documents',
  },
  {
    path: '/documents',
    name: 'DocumentList',
    component: () => import('../views/DocumentListView.vue'),
    meta: { title: '文档列表' },
  },
  {
    path: '/documents/new',
    name: 'DocumentUpload',
    component: () => import('../views/DocumentUploadView.vue'),
    meta: { title: '提交文档入库' },
  },
  {
    path: '/retrieval',
    name: 'RetrievalChat',
    component: () => import('../views/RetrievalChatView.vue'),
    meta: { title: '智能问答' },
  },
  {
    path: '/channels',
    name: 'ChannelStatus',
    component: () => import('../views/ChannelStatusView.vue'),
    meta: { title: '渠道状态' },
  },
  {
    path: '/auth/callback',
    name: 'AuthCallback',
    component: () => import('../views/AuthCallbackView.vue'),
    meta: { title: '登录验证', public: true },
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
    document.title = `${to.meta.title} - 企业知识库`;
  }

  // 免守卫白名单端点 (OIDC 回调等)
  if (to.meta.public || to.path === '/auth/callback') {
    return next();
  }

  const authStore = useAuthStore();
  if (!authStore.token) {
    try {
      await authStore.initAuth();
      if (!authStore.token) {
        sessionStorage.setItem('rag_auth_redirect', to.fullPath);
        window.location.href = '/oauth2/authorization/corp-sso';
        return;
      }
    } catch {
      sessionStorage.setItem('rag_auth_redirect', to.fullPath);
      window.location.href = '/oauth2/authorization/corp-sso';
      return;
    }
  }

  // 新建文档上传页需具备 doc:upload 权限；仅查看已有文档进度 (?doc_id=...) 则放行交由对象权限校验
  if (to.path === '/documents/new' && !to.query.doc_id && !authStore.hasPermission('doc:upload')) {
    return next('/documents');
  }

  next();
});
