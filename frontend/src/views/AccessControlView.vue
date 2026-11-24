<template>
  <div class="access-control-view h-full overflow-y-auto py-6 px-4 sm:px-8 max-w-7xl mx-auto space-y-6 animate-in fade-in duration-300">
    <!-- 顶部标题栏 -->
    <div class="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-sm flex flex-wrap items-center justify-between gap-4">
      <div>
        <h1 class="text-base font-bold text-slate-900 tracking-tight">知识库权限治理中心</h1>
      </div>
      <button
        @click="refreshAll"
        :disabled="loading"
        class="px-3.5 py-2 rounded-xl border border-slate-200 bg-white hover:bg-slate-50 text-xs font-medium text-slate-700 cursor-pointer disabled:opacity-50"
      >
        {{ loading ? '刷新中...' : '刷新数据' }}
      </button>
    </div>

    <div v-if="globalError" class="p-3.5 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700">
      {{ globalError }}
    </div>

    <!-- 治理功能标签（Tab 2 职级策略与 Tab 3 安全审计/封禁仅向 system:admin 开放） -->
    <div v-if="tabs.length > 1" class="flex flex-wrap items-center gap-2 border-b border-slate-200 pb-2 text-xs font-medium">
      <button
        v-for="t in tabs"
        :key="t.key"
        @click="activeTab = t.key"
        class="px-4 py-2 rounded-xl transition-colors cursor-pointer"
        :class="activeTab === t.key ? 'bg-slate-900 text-white shadow-xs' : 'bg-white text-slate-600 hover:bg-slate-100 border border-slate-200'"
      >
        {{ t.label }}
      </button>
    </div>

    <!-- TAB 1: 知识库与精细化 ACL -->
    <div v-if="activeTab === 'kbs'" class="space-y-5">
      <div v-if="canManageKb" class="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm space-y-4">
        <div class="text-xs font-bold text-slate-800">创建知识库容器</div>
        <div class="grid grid-cols-1 md:grid-cols-6 gap-3 text-xs">
          <input v-model="kbForm.name" type="text" placeholder="知识库名称（如：业财数字化协作库）" class="md:col-span-2 p-2 rounded-lg border border-slate-200 outline-none" />
          <select v-model="kbForm.ownerDepartmentId" class="p-2 rounded-lg border border-slate-200 bg-white outline-none">
            <option v-if="canManageAllOrg" value="">公共/全员可见 (/public)</option>
            <option v-for="d in manageableDepartments" :key="d.id" :value="d.id">{{ d.name }} ({{ d.path }})</option>
          </select>
          <select v-model="kbForm.visibility" class="p-2 rounded-lg border border-slate-200 bg-white outline-none">
            <option value="DEPARTMENT">部门专属知识库</option>
            <option v-if="canManageAllOrg" value="PUBLIC">全员公开知识库</option>
            <option value="RESTRICTED">指定授权知识库</option>
          </select>
          <div class="flex items-center gap-1">
            <select
              v-model.number="kbForm.minClassificationLevel"
              :disabled="kbForm.visibility === 'PUBLIC'"
              class="flex-1 p-2 rounded-lg border border-slate-200 bg-white outline-none disabled:bg-slate-50 disabled:text-slate-400"
            >
              <option :value="1">最低L1</option>
              <option :value="2">最低L2</option>
              <option :value="3">最低L3</option>
              <option :value="4">最低L4</option>
            </select>
            <select
              v-model.number="kbForm.maxDocLevel"
              :disabled="kbForm.visibility === 'PUBLIC'"
              class="flex-1 p-2 rounded-lg border border-slate-200 bg-white outline-none disabled:bg-slate-50 disabled:text-slate-400"
            >
              <option :value="1">最高L1</option>
              <option :value="2">最高L2</option>
              <option :value="3">最高L3</option>
              <option :value="4">最高L4</option>
            </select>
          </div>
          <button
            type="button"
            :disabled="creatingKb || !kbForm.name.trim()"
            @click="handleCreateKb"
            class="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white font-semibold disabled:opacity-50 cursor-pointer"
          >
            {{ creatingKb ? '创建中...' : '+ 新建知识库' }}
          </button>
        </div>
      </div>

      <div class="bg-white border border-slate-200 rounded-2xl overflow-hidden shadow-sm overflow-x-auto">
        <table class="w-full text-left text-xs">
          <thead class="bg-slate-50 text-[11px] text-slate-500 border-b border-slate-200 whitespace-nowrap">
            <tr>
              <th class="py-3 px-4">知识库名称</th>
              <th class="py-3 px-4">归属部门路径</th>
              <th class="py-3 px-4">可见性</th>
              <th class="py-3 px-4">密级范围</th>
              <th class="py-3 px-4 text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr v-for="kb in knowledgeBases" :key="kb.id" class="hover:bg-slate-50/70">
              <td class="py-3 px-4 font-semibold text-slate-900 whitespace-nowrap">{{ kb.name }}</td>
              <td class="py-3 px-4 font-mono text-[11px] text-slate-600 whitespace-nowrap">{{ kb.ownerDeptPath }}</td>
              <td class="py-3 px-4 text-[11px] text-slate-600 whitespace-nowrap">{{ formatVisibilityLabel(kb.visibility) }}</td>
              <td class="py-3 px-4 whitespace-nowrap">
                <div class="flex items-center gap-1">
                  <ClassificationBadge :level="kb.minClassificationLevel" />
                  <span class="text-slate-400">~</span>
                  <ClassificationBadge :level="kb.maxDocLevel" />
                </div>
              </td>
              <td class="py-3 px-4 text-right space-x-2 whitespace-nowrap">
                <button @click="openAclDrawer(kb)" class="px-2.5 py-1 rounded-lg bg-indigo-50 hover:bg-indigo-100 text-indigo-700 font-medium cursor-pointer">
                  访问授权
                </button>
                <button
                  v-if="canManageKb"
                  @click="handleDeleteKb(kb)"
                  class="px-2.5 py-1 rounded-lg text-rose-600 hover:bg-rose-50 font-medium cursor-pointer"
                >
                  删除
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- TAB 2: 职级密级与角色策略映射（仅 system:admin 可见） -->
    <div v-if="activeTab === 'policy' && isSystemAdmin" class="space-y-6">
      <PositionLevelPanel :positions="positions" @changed="refreshAll" />

      <div class="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm space-y-3">
        <h3 class="text-sm font-bold text-slate-900">企业角色权限与数据范围</h3>
        <div class="border border-slate-200 rounded-xl overflow-hidden overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-slate-50 text-[11px] text-slate-500 border-b border-slate-200 whitespace-nowrap">
              <tr>
                <th class="py-2.5 px-4">企业角色编码</th>
                <th class="py-2.5 px-4">角色名称</th>
                <th class="py-2.5 px-4">数据范围</th>
                <th class="py-2.5 px-4">功能权限集合</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              <tr v-for="r in roles" :key="r.roleCode">
                <td class="py-2.5 px-4 font-mono font-semibold text-slate-800 whitespace-nowrap">{{ r.roleCode }}</td>
                <td class="py-2.5 px-4 text-slate-700 whitespace-nowrap">{{ r.roleName }}</td>
                <td class="py-2.5 px-4 text-[11px] text-indigo-700 whitespace-nowrap">{{ formatDataScopeLabel(r.dataScope) }}</td>
                <td class="py-2.5 px-4 text-[11px] text-slate-500">{{ formatPermissionsLabel(r.permissionsJson) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm space-y-3">
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-bold text-slate-900">企业部门目录</h3>
          <span class="text-xs text-slate-400">共 {{ departments.length }} 个部门</span>
        </div>
        <div class="flex flex-wrap gap-2 text-xs">
          <span v-for="d in departments" :key="d.id" class="px-2.5 py-1 rounded-lg bg-slate-100 text-slate-700 font-mono">
            {{ d.name }} · {{ d.path }}
          </span>
        </div>
      </div>
    </div>

    <!-- TAB 3: 安全审计与应急封禁（仅 system:admin 可见） -->
    <div v-if="activeTab === 'audit' && isSystemAdmin" class="space-y-6">
      <div class="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm space-y-3">
        <h3 class="text-sm font-bold text-slate-900">员工账号与应急停用</h3>
        <div class="border border-slate-200 rounded-xl overflow-hidden overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-slate-50 text-[11px] text-slate-500 border-b border-slate-200 whitespace-nowrap">
              <tr>
                <th class="py-2.5 px-4">员工账号</th>
                <th class="py-2.5 px-4">职级与密级</th>
                <th class="py-2.5 px-4">所属部门</th>
                <th class="py-2.5 px-4">状态</th>
                <th class="py-2.5 px-4 text-right">应急风控</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              <tr v-for="u in users" :key="u.userId">
                <td class="py-2.5 px-4 font-semibold text-slate-900 whitespace-nowrap">{{ u.username }}</td>
                <td class="py-2.5 px-4 whitespace-nowrap">
                  <div class="flex items-center gap-2">
                    <span>{{ u.positionName }}</span>
                    <ClassificationBadge :level="u.effectiveClearanceLevel" />
                  </div>
                </td>
                <td class="py-2.5 px-4 font-mono text-[11px] text-slate-500 whitespace-nowrap">{{ u.effectiveDeptPaths.join(', ') || '-' }}</td>
                <td class="py-2.5 px-4 text-[11px] font-medium whitespace-nowrap" :class="u.isActive ? 'text-emerald-600' : 'text-rose-600'">
                  {{ u.isActive ? '正常' : '已停用' }}
                </td>
                <td class="py-2.5 px-4 text-right whitespace-nowrap">
                  <button v-if="u.isActive" @click="handleRevokeUser(u)" class="text-rose-600 hover:underline font-medium cursor-pointer">停用吊销</button>
                  <button v-else @click="handleRestoreUser(u)" class="text-emerald-600 hover:underline font-medium cursor-pointer">恢复启用</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm space-y-3">
        <div class="flex flex-wrap items-center gap-3 text-xs">
          <input v-model="auditFilter.action" type="text" placeholder="按操作类型过滤..." class="flex-1 min-w-[220px] p-2 rounded-lg border border-slate-200 outline-none" />
          <select v-model="auditFilter.status" class="p-2 rounded-lg border border-slate-200 bg-white outline-none">
            <option value="">全部结果</option>
            <option value="SUCCESS">成功</option>
            <option value="DENIED">已拒绝</option>
            <option value="FAILED">失败</option>
          </select>
          <button @click="loadAuditLogs" class="px-4 py-2 rounded-lg bg-slate-900 text-white font-semibold cursor-pointer">查询审计</button>
        </div>

        <div class="border border-slate-200 rounded-xl overflow-hidden overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-slate-50 text-[11px] text-slate-500 border-b border-slate-200 whitespace-nowrap">
              <tr>
                <th class="py-2.5 px-4">时间</th>
                <th class="py-2.5 px-4">动作</th>
                <th class="py-2.5 px-4">操作人</th>
                <th class="py-2.5 px-4">状态</th>
                <th class="py-2.5 px-4">详情</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              <tr v-if="auditLogs.length === 0">
                <td colspan="5" class="py-8 text-center text-slate-400">暂无审计记录</td>
              </tr>
              <tr v-for="log in auditLogs" :key="log.id">
                <td class="py-2.5 px-4 font-mono text-[11px] text-slate-500 whitespace-nowrap">{{ log.createdAt ? new Date(log.createdAt).toLocaleString('zh-CN') : '-' }}</td>
                <td class="py-2.5 px-4 font-mono font-medium text-slate-800 whitespace-nowrap">{{ log.action }}</td>
                <td class="py-2.5 px-4 text-[11px] text-slate-700 whitespace-nowrap" :title="log.userId || ''">{{ formatAuditOperator(log.userId) }}</td>
                <td class="py-2.5 px-4 text-[11px] whitespace-nowrap" :class="log.status === 'SUCCESS' ? 'text-emerald-600' : log.status === 'DENIED' ? 'text-amber-600' : 'text-rose-600'">
                  {{ formatAuditStatus(log.status) }}
                </td>
                <td class="py-2.5 px-4 text-[11px] text-slate-500 max-w-md truncate" :title="log.errorMsg || log.detailsJson">{{ log.errorMsg || log.detailsJson }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <KnowledgeBaseAclDrawer
      :visible="aclDrawerVisible"
      :knowledgeBase="selectedKb"
      :departments="departments"
      :users="users"
      :roles="roles"
      @close="aclDrawerVisible = false"
      @changed="refreshAll"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, reactive, watch, onMounted } from 'vue';
import { accessControlApi } from '../api/accessControl';
import { useAuthStore, PERMISSIONS } from '../stores/auth';
import type {
  AdminAuditLog,
  AdminDepartment,
  AdminKnowledgeBase,
  AdminPosition,
  AdminRole,
  AdminUserSummary,
  KbVisibility,
} from '../types/authz';
import ClassificationBadge from '../components/common/ClassificationBadge.vue';
import KnowledgeBaseAclDrawer from '../components/access/KnowledgeBaseAclDrawer.vue';
import PositionLevelPanel from '../components/access/PositionLevelPanel.vue';

type GovernanceTabKey = 'kbs' | 'policy' | 'audit';

const authStore = useAuthStore();
const isSystemAdmin = computed(() => authStore.hasPermission(PERMISSIONS.SYSTEM_ADMIN));
const canManageKb = computed(() => authStore.hasAnyPermission([PERMISSIONS.KB_MANAGE, PERMISSIONS.SYSTEM_ADMIN]));
const canManageAllOrg = computed(
  () => isSystemAdmin.value || authStore.currentUser?.scope_by_action?.MANAGE === 'ALL_ORG'
);

const tabs = computed<Array<{ key: GovernanceTabKey; label: string }>>(() => {
  const list: Array<{ key: GovernanceTabKey; label: string }> = [
    { key: 'kbs', label: '知识库与访问授权' },
  ];
  if (isSystemAdmin.value) {
    list.push(
      { key: 'policy', label: '职级密级与角色策略' },
      { key: 'audit', label: '安全审计与应急停用' }
    );
  }
  return list;
});

const activeTab = ref<GovernanceTabKey>('kbs');
const loading = ref(false);
const globalError = ref('');

const departments = ref<AdminDepartment[]>([]);
const positions = ref<AdminPosition[]>([]);
const roles = ref<AdminRole[]>([]);
const users = ref<AdminUserSummary[]>([]);
const knowledgeBases = ref<AdminKnowledgeBase[]>([]);
const auditLogs = ref<AdminAuditLog[]>([]);

const VISIBILITY_LABEL_MAP: Record<string, string> = {
  PUBLIC: '全员公开',
  DEPARTMENT: '部门专属',
  RESTRICTED: '指定授权',
};

const DATA_SCOPE_LABEL_MAP: Record<string, string> = {
  ALL_ORG: '全公司',
  DEPT_AND_SUB: '本部门及下级',
  CUSTOM_ACL: '仅显式授权',
};

const PERM_LABEL_MAP: Record<string, string> = {
  '*': '全部系统权限',
  'kb:retrieve': '知识检索',
  'doc:upload': '文档上传',
  'doc:delete': '文档删除',
  'doc:review': '解析审核',
  'kb:manage': '知识库管理',
  'kb:grant': '访问授权',
  'user:authz:manage': '人员权限治理',
  'security:classify': '文档定密/升密',
  'security:declassify': '文档降密',
  'system:admin': '系统管理',
};

const userNameById = computed(() => {
  const map = new Map<string, string>();
  for (const u of users.value) {
    map.set(u.userId, u.username);
  }
  return map;
});

const formatVisibilityLabel = (v: string): string => VISIBILITY_LABEL_MAP[v] || v;
const formatDataScopeLabel = (scope: string): string => DATA_SCOPE_LABEL_MAP[scope] || scope;
const formatPermissionsLabel = (jsonStr: string): string => {
  try {
    const arr = JSON.parse(jsonStr);
    if (Array.isArray(arr)) {
      return arr.map((p) => PERM_LABEL_MAP[String(p)] || String(p)).join('、') || '-';
    }
  } catch {
    // ignore parse error
  }
  return jsonStr || '-';
};
const formatAuditOperator = (userId?: string | null): string => {
  if (!userId) return '-';
  return userNameById.value.get(userId) || userId;
};
const formatAuditStatus = (s: string): string => {
  if (s === 'SUCCESS') return '成功';
  if (s === 'DENIED') return '已拒绝';
  if (s === 'FAILED') return '失败';
  return s;
};

const isDeptUnderPrefix = (path: string, prefix: string): boolean =>
  path === prefix || path.startsWith(prefix + '/');

const manageableDepartments = computed(() => {
  const activeDepts = departments.value.filter((d) => d.status === 'ACTIVE');
  if (canManageAllOrg.value) {
    return activeDepts;
  }
  const userDepts = (authStore.currentUser?.dept_paths || []).filter(Boolean);
  if (userDepts.length === 0) return [];
  const manageScope = authStore.currentUser?.scope_by_action?.MANAGE;
  if (manageScope === 'DEPT_AND_SUB') {
    return activeDepts.filter((d) => userDepts.some((prefix) => isDeptUnderPrefix(d.path, prefix)));
  }
  return [];
});

const creatingKb = ref(false);
const kbForm = reactive<{
  name: string;
  ownerDepartmentId: string;
  visibility: KbVisibility;
  minClassificationLevel: number;
  maxDocLevel: number;
}>({ name: '', ownerDepartmentId: '', visibility: 'DEPARTMENT', minClassificationLevel: 1, maxDocLevel: 4 });

watch(
  () => kbForm.visibility,
  (vis) => {
    if (vis === 'PUBLIC') {
      kbForm.minClassificationLevel = 1;
      kbForm.maxDocLevel = 1;
    } else if (kbForm.maxDocLevel === 1) {
      kbForm.maxDocLevel = 4;
    }
  }
);

watch(
  manageableDepartments,
  (list) => {
    if (!canManageAllOrg.value && list.length > 0 && !list.some((d) => d.id === kbForm.ownerDepartmentId)) {
      kbForm.ownerDepartmentId = list[0].id;
    }
  },
  { immediate: true }
);

const auditFilter = reactive({ action: '', status: '' });
const aclDrawerVisible = ref(false);
const selectedKb = ref<AdminKnowledgeBase | null>(null);

const loadAuditLogs = async () => {
  if (!isSystemAdmin.value) {
    auditLogs.value = [];
    return;
  }
  try {
    auditLogs.value = await accessControlApi.listAuditLogs({
      action: auditFilter.action.trim() || undefined,
      status: auditFilter.status || undefined,
      limit: 100,
    });
  } catch (err: any) {
    globalError.value = err.message || '加载审计日志失败';
  }
};

const refreshAll = async () => {
  loading.value = true;
  globalError.value = '';
  try {
    const [depts, roleList, userList, kbList, posList] = await Promise.all([
      accessControlApi.listDepartments(),
      accessControlApi.listRoles(),
      accessControlApi.listUsers(),
      accessControlApi.listKnowledgeBases(),
      isSystemAdmin.value ? accessControlApi.listPositions() : Promise.resolve<AdminPosition[]>([]),
    ]);
    departments.value = depts;
    roles.value = roleList;
    users.value = userList;
    knowledgeBases.value = kbList;
    positions.value = posList;
    if (isSystemAdmin.value) {
      await loadAuditLogs();
    } else {
      auditLogs.value = [];
      if (activeTab.value !== 'kbs') {
        activeTab.value = 'kbs';
      }
    }
  } catch (err: any) {
    globalError.value = err.message || '加载治理数据失败';
  } finally {
    loading.value = false;
  }
};

const handleCreateKb = async () => {
  if (!kbForm.name.trim()) return;
  creatingKb.value = true;
  globalError.value = '';
  try {
    const isPublic = kbForm.visibility === 'PUBLIC';
    const minL = isPublic ? 1 : kbForm.minClassificationLevel;
    const maxL = isPublic ? 1 : Math.max(minL, kbForm.maxDocLevel);
    const defaultL = isPublic ? 1 : Math.min(Math.max(2, minL), maxL);
    await accessControlApi.createKnowledgeBase({
      name: kbForm.name.trim(),
      ownerDepartmentId: kbForm.ownerDepartmentId || undefined,
      ownerDeptPath: kbForm.ownerDepartmentId ? undefined : '/public',
      visibility: kbForm.visibility,
      minClassificationLevel: minL,
      defaultDocLevel: defaultL,
      maxDocLevel: maxL,
      reason: '治理中心创建知识库',
    });
    kbForm.name = '';
    await refreshAll();
  } catch (err: any) {
    globalError.value = err.message || '创建知识库失败';
  } finally {
    creatingKb.value = false;
  }
};

const handleDeleteKb = async (kb: AdminKnowledgeBase) => {
  if (!confirm(`确认删除空知识库《${kb.name}》吗？`)) return;
  try {
    await accessControlApi.deleteKnowledgeBase(kb.id);
    await refreshAll();
  } catch (err: any) {
    globalError.value = err.message || '删除知识库失败';
  }
};

const openAclDrawer = (kb: AdminKnowledgeBase) => {
  selectedKb.value = kb;
  aclDrawerVisible.value = true;
};

const handleRevokeUser = async (u: AdminUserSummary) => {
  if (!confirm(`确认应急停用并吊销员工 ${u.username} 的在线会话吗？`)) return;
  try {
    await accessControlApi.revokeUser(u.userId, '应急离职/风控封禁');
    await refreshAll();
  } catch (err: any) {
    globalError.value = err.message || '停用账号失败';
  }
};

const handleRestoreUser = async (u: AdminUserSummary) => {
  try {
    await accessControlApi.restoreUser(u.userId);
    await refreshAll();
  } catch (err: any) {
    globalError.value = err.message || '恢复账号失败';
  }
};

onMounted(() => {
  refreshAll();
});
</script>
