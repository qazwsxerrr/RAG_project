import { createRouter, createWebHistory } from 'vue-router';
import { useAuthStore, PERMISSIONS, GOVERNANCE_PERMISSIONS } from '../stores/auth';

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
    path: '/access-control',
    name: 'AccessControl',
    component: () => import('../views/AccessControlView.vue'),
    meta: { title: '权限治理中心', requiredAnyPermissions: GOVERNANCE_PERMISSIONS },
  },
  {
    path: '/auth/callback',
    name: 'AuthCallback',
    component: () => import('../views/AuthCallbackView.vue'),
    meta: { title: '登录验证', public: true },
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

  if (to.meta.public || to.path === '/auth/callback') {
    return next();
  }

  const authStore = useAuthStore();
  if (!authStore.token) {
    try {
      await authStore.refreshToken({ redirectOnUnauthorized: false });
    } catch {
      // authStore.authError 已记录失败原因
    }
    if (!authStore.token) {
      const errStatus = authStore.authError?.status;
      if (errStatus !== 401 || authStore.authError?.isLoopBlocked) {
        sessionStorage.setItem('rag_auth_redirect', to.fullPath);
        return next('/auth/callback');
      }
      if (!authStore.redirectToSso({ redirectPath: to.fullPath })) {
        return next('/auth/callback');
      }
      return;
    }
  }

  if (to.path === '/documents/new' && !to.query.doc_id && !authStore.hasPermission(PERMISSIONS.DOC_UPLOAD)) {
    return next('/documents');
  }

  if (typeof to.meta.requiredPermission === 'string' && !authStore.hasPermission(to.meta.requiredPermission)) {
    return next('/documents');
  }

  if (Array.isArray(to.meta.requiredAnyPermissions) && !authStore.hasAnyPermission(to.meta.requiredAnyPermissions)) {
    return next('/documents');
  }

  next();
});
