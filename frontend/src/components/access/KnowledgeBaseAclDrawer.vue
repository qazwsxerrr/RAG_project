<template>
  <div
    v-if="visible && knowledgeBase"
    class="fixed inset-0 z-50 flex justify-end bg-slate-900/35 backdrop-blur-xs"
    @click.self="$emit('close')"
  >
    <div class="w-full max-w-2xl bg-white h-full shadow-2xl border-l border-slate-200 flex flex-col">
      <!-- 顶部标题 -->
      <div class="px-6 py-4 border-b border-slate-200 flex items-center justify-between">
        <div>
          <h3 class="text-sm font-bold text-slate-900">知识库访问授权管理</h3>
          <p class="text-xs text-slate-500 mt-0.5">
            {{ knowledgeBase.name }} · {{ knowledgeBase.ownerDeptPath }}
          </p>
        </div>
        <button @click="$emit('close')" class="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 cursor-pointer">
          ✕
        </button>
      </div>

      <!-- 主体区域 -->
      <div class="flex-1 overflow-y-auto p-6 space-y-6">
        <!-- 只读提示（无 kb:grant / system:admin 写权限时） -->
        <div
          v-if="!canGrant"
          class="p-3.5 rounded-xl bg-amber-50 border border-amber-200 text-xs text-amber-800"
        >
          当前账号仅具备本知识库授权规则的只读查看权限，如需新增或撤销访问规则，请联系具备 <span class="font-mono font-semibold">kb:grant</span> 权限的管理员。
        </div>

        <!-- 新增授权表单（仅具备 kb:grant 或 system:admin 时展示） -->
        <div v-if="canGrant" class="p-4 rounded-xl bg-slate-50 border border-slate-200 space-y-4">
          <div class="text-xs font-bold text-slate-800">新增访问规则（禁止优先于允许）</div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
            <div class="space-y-1">
              <label class="font-medium text-slate-600">主体类型</label>
              <select v-model="grantForm.subjectType" class="w-full p-2 rounded-lg border border-slate-200 bg-white outline-none">
                <option value="DEPARTMENT">部门</option>
                <option value="USER">指定员工</option>
                <option value="ROLE" :disabled="!isOrgWideGranter">
                  角色{{ isOrgWideGranter ? '' : '（需全公司管理员）' }}
                </option>
              </select>
            </div>

            <div class="space-y-1">
              <label class="font-medium text-slate-600">目标主体</label>
              <select
                v-if="grantForm.subjectType === 'DEPARTMENT'"
                v-model="grantForm.subjectId"
                class="w-full p-2 rounded-lg border border-slate-200 bg-white outline-none"
              >
                <option value="">请选择部门...</option>
                <option v-for="d in scopedDepartments" :key="d.id" :value="d.id">{{ d.name }} ({{ d.path }})</option>
              </select>
              <select
                v-else-if="grantForm.subjectType === 'USER'"
                v-model="grantForm.subjectId"
                class="w-full p-2 rounded-lg border border-slate-200 bg-white outline-none"
              >
                <option value="">请选择员工...</option>
                <option v-for="u in users" :key="u.userId" :value="u.userId">
                  {{ u.username }} ({{ u.primaryDeptPath || '-' }} · {{ u.positionName }} · L{{ u.effectiveClearanceLevel }})
                </option>
              </select>
              <select
                v-else
                v-model="grantForm.subjectId"
                class="w-full p-2 rounded-lg border border-slate-200 bg-white outline-none"
              >
                <option value="">请选择角色...</option>
                <option v-for="r in roles" :key="r.roleCode" :value="r.roleCode">{{ r.roleName }} ({{ r.roleCode }})</option>
              </select>
            </div>

            <div class="space-y-1">
              <label class="font-medium text-slate-600">动作权限</label>
              <select v-model="grantForm.action" class="w-full p-2 rounded-lg border border-slate-200 bg-white outline-none">
                <option value="READ">查阅与检索</option>
                <option value="UPLOAD">上传入库</option>
                <option value="REVIEW">人工审核</option>
                <option value="DELETE">删除文档</option>
                <option value="MANAGE" :disabled="isCrossDeptUserSelected">
                  管理与调密{{ isCrossDeptUserSelected ? '（跨部门不可转授）' : '' }}
                </option>
              </select>
            </div>

            <div class="space-y-1">
              <label class="font-medium text-slate-600">规则效力</label>
              <select v-model="grantForm.effect" class="w-full p-2 rounded-lg border border-slate-200 bg-white outline-none">
                <option value="ALLOW">允许访问</option>
                <option value="DENY">禁止访问（优先拦截）</option>
              </select>
            </div>

            <div class="space-y-1">
              <label class="font-medium text-slate-600">过期时间（可选，留空为长期）</label>
              <input
                v-model="grantForm.expiresAtLocal"
                type="datetime-local"
                class="w-full p-2 rounded-lg border border-slate-200 bg-white outline-none"
              />
            </div>

            <div class="space-y-1 flex flex-col justify-end">
              <label
                v-if="grantForm.subjectType === 'DEPARTMENT'"
                class="inline-flex items-center gap-2 py-2 cursor-pointer"
              >
                <input type="checkbox" v-model="grantForm.includeSubDepts" class="rounded border-slate-300 text-blue-600" />
                <span class="text-slate-700">包含所有下级子部门</span>
              </label>
            </div>

            <div
              v-if="isCrossDeptUserSelected"
              class="sm:col-span-2 p-2.5 rounded-lg bg-indigo-50 border border-indigo-200 text-[11px] text-indigo-800"
            >
              跨部门点对点协作：可向该同事授予查阅、上传、审核或删除权限，不支持跨部门转授知识库管理权 (MANAGE)。
            </div>

            <div class="sm:col-span-2 space-y-1">
              <label class="font-medium text-slate-600">授权原因（审计留痕）</label>
              <input
                v-model="grantForm.reason"
                type="text"
                placeholder="例如：业财一体化项目组跨部门联调只读授权"
                class="w-full p-2 rounded-lg border border-slate-200 bg-white outline-none"
              />
            </div>
          </div>

          <div v-if="errorMsg" class="p-2.5 rounded-lg bg-rose-50 border border-rose-200 text-xs text-rose-700">
            {{ errorMsg }}
          </div>

          <div class="flex justify-end">
            <button
              type="button"
              :disabled="submitting || !grantForm.subjectId"
              @click="handleAddGrant"
              class="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold disabled:opacity-50 cursor-pointer"
            >
              {{ submitting ? '写入中...' : '+ 添加授权规则' }}
            </button>
          </div>
        </div>

        <!-- 已有规则列表 -->
        <div class="space-y-2.5">
          <div class="flex items-center justify-between">
            <span class="text-xs font-bold text-slate-800">当前及历史授权规则 ({{ grants.length }})</span>
            <button @click="loadGrants" class="text-xs text-blue-600 hover:text-blue-800 cursor-pointer">刷新</button>
          </div>

          <div v-if="loading" class="py-8 text-center text-xs text-slate-400">正在加载授权规则...</div>
          <div v-else-if="grants.length === 0" class="py-8 text-center text-xs text-slate-400 border border-dashed border-slate-200 rounded-xl">
            当前知识库暂无专项授权规则
          </div>

          <div v-else class="border border-slate-200 rounded-xl overflow-hidden">
            <table class="w-full text-left text-xs">
              <thead class="bg-slate-50 text-[11px] text-slate-500 border-b border-slate-200">
                <tr>
                  <th class="py-2.5 px-3">主体</th>
                  <th class="py-2.5 px-3">动作</th>
                  <th class="py-2.5 px-3">效力</th>
                  <th class="py-2.5 px-3">过期时间</th>
                  <th v-if="canGrant" class="py-2.5 px-3 text-right">操作</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-100">
                <tr v-for="g in grants" :key="g.id" class="hover:bg-slate-50/70">
                  <td class="py-2.5 px-3">
                    <div class="font-medium text-slate-800">{{ formatSubjectLabel(g) }}</div>
                    <div class="text-[10px] text-slate-400">
                      {{ g.subjectType }}
                      <span v-if="g.subjectType === 'DEPARTMENT' && g.includeSubDepts"> · 含子部门</span>
                      <span v-if="g.reason"> · {{ g.reason }}</span>
                    </div>
                  </td>
                  <td class="py-2.5 px-3 font-mono text-[11px]">{{ g.action }}</td>
                  <td class="py-2.5 px-3">
                    <span
                      class="px-2 py-0.5 rounded text-[11px] font-semibold"
                      :class="g.effect === 'DENY' ? 'bg-rose-50 text-rose-700 border border-rose-200' : 'bg-emerald-50 text-emerald-700 border border-emerald-200'"
                    >
                      {{ g.effect }}
                    </span>
                  </td>
                  <td class="py-2.5 px-3 text-[11px] text-slate-500 font-mono">
                    {{ g.expiresAt ? new Date(g.expiresAt).toLocaleString('zh-CN') : '长期有效' }}
                  </td>
                  <td v-if="canGrant" class="py-2.5 px-3 text-right">
                    <button
                      v-if="canRevokeGrant(g)"
                      @click="handleDeleteGrant(g)"
                      class="text-rose-600 hover:text-rose-800 text-xs font-medium cursor-pointer"
                    >
                      撤销
                    </button>
                    <span v-else class="text-[11px] text-slate-400">仅全公司管理员</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, reactive, watch } from 'vue';
import { accessControlApi } from '../../api/accessControl';
import { useAuthStore, PERMISSIONS } from '../../stores/auth';
import type {
  AdminDepartment,
  AdminKbGrant,
  AdminKnowledgeBase,
  AdminRole,
  AdminUserSummary,
  GrantEffect,
  GrantSubjectType,
  KbAction,
} from '../../types/authz';

const props = defineProps<{
  visible: boolean;
  knowledgeBase: AdminKnowledgeBase | null;
  departments: AdminDepartment[];
  users: AdminUserSummary[];
  roles: AdminRole[];
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'changed'): void;
}>();

const authStore = useAuthStore();
const canGrant = computed(() =>
  authStore.hasAnyPermission([PERMISSIONS.KB_GRANT, PERMISSIONS.SYSTEM_ADMIN])
);
const isSystemAdmin = computed(() => authStore.hasPermission(PERMISSIONS.SYSTEM_ADMIN));
const manageScope = computed(() => authStore.currentUser?.scope_by_action?.MANAGE);
const isOrgWideGranter = computed(
  () => isSystemAdmin.value || manageScope.value === 'ALL_ORG'
);

const isDeptPathUnderPrefix = (path: string, prefix: string): boolean =>
  path === prefix || path.startsWith(prefix + '/');

const scopedDepartments = computed(() => {
  const activeDepts = props.departments.filter((d) => d.status === 'ACTIVE');
  if (isOrgWideGranter.value) return activeDepts;
  const userDepts = (authStore.currentUser?.dept_paths_by_action?.MANAGE || []).filter(Boolean);
  if (userDepts.length === 0) return [];
  if (manageScope.value === 'DEPT_AND_SUB') {
    return activeDepts.filter((d) => userDepts.some((prefix) => isDeptPathUnderPrefix(d.path, prefix)));
  }
  return [];
});

const isDeptPathInManageScope = (targetPath?: string | null): boolean => {
  if (isOrgWideGranter.value) return true;
  if (!targetPath) return false;
  const userDepts = (authStore.currentUser?.dept_paths_by_action?.MANAGE || []).filter(Boolean);
  if (userDepts.length === 0) return false;
  if (manageScope.value === 'DEPT_AND_SUB') {
    return userDepts.some((prefix) => isDeptPathUnderPrefix(targetPath, prefix));
  }
  return false;
};

const isUserInManageScope = (u: AdminUserSummary): boolean => {
  if (isOrgWideGranter.value) return true;
  const targetPaths = (u.effectiveDeptPaths || []).filter(Boolean);
  if (targetPaths.length === 0) return false;
  return targetPaths.some((p) => isDeptPathInManageScope(p));
};

const canRevokeGrant = (g: AdminKbGrant): boolean => {
  if (isOrgWideGranter.value) return true;
  if (g.subjectType === 'ROLE') return false;
  if (g.subjectType === 'DEPARTMENT') {
    const dept = props.departments.find((d) => d.id === g.subjectId);
    return isDeptPathInManageScope(dept?.path || g.subjectDeptPath);
  }
  if (g.subjectType === 'USER' && g.action === 'MANAGE') {
    const targetUser = props.users.find((u) => u.userId === g.subjectId);
    return targetUser ? isUserInManageScope(targetUser) : false;
  }
  return true;
};

const grants = ref<AdminKbGrant[]>([]);
const loading = ref(false);
const submitting = ref(false);
const errorMsg = ref('');

const grantForm = reactive<{
  subjectType: GrantSubjectType;
  subjectId: string;
  includeSubDepts: boolean;
  action: KbAction;
  effect: GrantEffect;
  expiresAtLocal: string;
  reason: string;
}>({
  subjectType: 'DEPARTMENT',
  subjectId: '',
  includeSubDepts: true,
  action: 'READ',
  effect: 'ALLOW',
  expiresAtLocal: '',
  reason: '',
});

const selectedTargetUser = computed(
  () => props.users.find((u) => u.userId === grantForm.subjectId) || null
);
const isCrossDeptUserSelected = computed(
  () =>
    grantForm.subjectType === 'USER' &&
    selectedTargetUser.value !== null &&
    !isUserInManageScope(selectedTargetUser.value)
);

watch(isCrossDeptUserSelected, (cross) => {
  if (cross && grantForm.action === 'MANAGE') {
    grantForm.action = 'READ';
  }
});

const loadGrants = async () => {
  if (!props.knowledgeBase) return;
  loading.value = true;
  errorMsg.value = '';
  try {
    grants.value = await accessControlApi.listKbGrants(props.knowledgeBase.id);
  } catch (err: any) {
    errorMsg.value = err.message || '加载 ACL 失败';
  } finally {
    loading.value = false;
  }
};

watch(
  () => [props.visible, props.knowledgeBase?.id],
  ([vis]) => {
    if (vis && props.knowledgeBase) {
      grantForm.subjectId = '';
      grantForm.reason = '';
      grantForm.expiresAtLocal = '';
      loadGrants();
    }
  }
);

watch(
  () => grantForm.subjectType,
  () => {
    grantForm.subjectId = '';
  }
);

const formatSubjectLabel = (g: AdminKbGrant): string => {
  if (g.subjectType === 'DEPARTMENT') {
    const dept = props.departments.find((d) => d.id === g.subjectId);
    return dept ? `${dept.name} (${dept.path})` : g.subjectDeptPath || g.subjectId;
  }
  if (g.subjectType === 'USER') {
    const u = props.users.find((item) => item.userId === g.subjectId);
    return u ? `${u.username} (${u.positionName})` : g.subjectId;
  }
  const r = props.roles.find((item) => item.roleCode === g.subjectId);
  return r ? `${r.roleName} (${r.roleCode})` : g.subjectId;
};

const handleAddGrant = async () => {
  if (!canGrant.value || !props.knowledgeBase || !grantForm.subjectId) return;
  submitting.value = true;
  errorMsg.value = '';
  try {
    await accessControlApi.createKbGrant(props.knowledgeBase.id, {
      subjectType: grantForm.subjectType,
      subjectId: grantForm.subjectId,
      includeSubDepts:
        grantForm.subjectType === 'DEPARTMENT'
          ? grantForm.includeSubDepts
          : false,
      action: grantForm.action,
      effect: grantForm.effect,
      expiresAt: grantForm.expiresAtLocal ? new Date(grantForm.expiresAtLocal).toISOString() : null,
      reason: grantForm.reason.trim() || undefined,
    });
    grantForm.reason = '';
    grantForm.expiresAtLocal = '';
    await loadGrants();
    emit('changed');
  } catch (err: any) {
    errorMsg.value = err.message || '添加 ACL 规则失败';
  } finally {
    submitting.value = false;
  }
};

const handleDeleteGrant = async (g: AdminKbGrant) => {
  if (!canGrant.value || !props.knowledgeBase) return;
  if (!confirm(`确认撤销该 ${g.effect} ${g.action} 授权规则吗？`)) return;
  try {
    await accessControlApi.deleteKbGrant(props.knowledgeBase.id, g.id);
    await loadGrants();
    emit('changed');
  } catch (err: any) {
    errorMsg.value = err.message || '撤销 ACL 失败';
  }
};
</script>
