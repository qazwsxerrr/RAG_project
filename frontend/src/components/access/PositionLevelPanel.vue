<template>
  <div class="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm space-y-5">
    <div class="border-b border-slate-100 pb-3">
      <h3 class="text-sm font-bold text-slate-900">职位等级与默认密级配置</h3>
      <p class="text-xs text-slate-500 mt-0.5">
        调整职级默认密级后将即时生效于对应职级员工
      </p>
    </div>

    <div v-if="errorMsg" class="p-2.5 rounded-lg bg-rose-50 border border-rose-200 text-xs text-rose-700">{{ errorMsg }}</div>
    <div v-if="successMsg" class="p-2.5 rounded-lg bg-emerald-50 border border-emerald-200 text-xs text-emerald-700">{{ successMsg }}</div>

    <div class="border border-slate-200 rounded-xl overflow-hidden overflow-x-auto">
      <table class="w-full text-left text-xs">
        <thead class="bg-slate-50 text-[11px] text-slate-500 border-b border-slate-200 whitespace-nowrap">
          <tr>
            <th class="py-2.5 px-4">职级编码</th>
            <th class="py-2.5 px-4">职级名称</th>
            <th class="py-2.5 px-4">排序</th>
            <th class="py-2.5 px-4">默认访问密级上限</th>
            <th class="py-2.5 px-4 text-right">操作</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          <tr v-for="pos in editablePositions" :key="pos.positionCode" class="hover:bg-slate-50/70">
            <td class="py-2.5 px-4 font-mono font-semibold text-slate-800 whitespace-nowrap">{{ pos.positionCode }}</td>
            <td class="py-2.5 px-4">
              <input v-model="pos.positionName" type="text" class="p-1.5 rounded border border-slate-200 bg-white w-36 outline-none" />
            </td>
            <td class="py-2.5 px-4">
              <input v-model.number="pos.rankOrder" type="number" class="p-1.5 rounded border border-slate-200 bg-white w-16 font-mono outline-none" />
            </td>
            <td class="py-2.5 px-4 whitespace-nowrap">
              <div class="flex items-center gap-2.5">
                <select v-model.number="pos.defaultClearanceLevel" class="p-1.5 rounded border border-slate-200 bg-white min-w-[132px] whitespace-nowrap outline-none">
                  <option :value="1" :disabled="1 < pos.minAllowedClearance || 1 > pos.maxAllowedClearance">L1 全员公开</option>
                  <option :value="2" :disabled="2 < pos.minAllowedClearance || 2 > pos.maxAllowedClearance">L2 部门内部</option>
                  <option :value="3" :disabled="3 < pos.minAllowedClearance || 3 > pos.maxAllowedClearance">L3 管理层机密</option>
                  <option :value="4" :disabled="4 < pos.minAllowedClearance || 4 > pos.maxAllowedClearance">L4 核心绝密</option>
                </select>
                <ClassificationBadge :level="pos.defaultClearanceLevel" />
                <span class="text-[11px] text-slate-400 font-mono">[L{{ pos.minAllowedClearance }}~L{{ pos.maxAllowedClearance }}]</span>
              </div>
            </td>
            <td class="py-2.5 px-4 text-right">
              <button
                type="button"
                :disabled="savingCode === pos.positionCode"
                @click="savePosition(pos)"
                class="px-3 py-1 rounded-lg bg-slate-900 hover:bg-slate-800 text-white text-xs font-medium disabled:opacity-50 cursor-pointer"
              >
                {{ savingCode === pos.positionCode ? '保存中...' : '保存' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { accessControlApi } from '../../api/accessControl';
import type { AdminPosition } from '../../types/authz';
import ClassificationBadge from '../common/ClassificationBadge.vue';

const props = defineProps<{
  positions: AdminPosition[];
}>();

const emit = defineEmits<{
  (e: 'changed'): void;
}>();

const editablePositions = ref<AdminPosition[]>([]);
const savingCode = ref('');
const errorMsg = ref('');
const successMsg = ref('');

watch(
  () => props.positions,
  (list) => {
    editablePositions.value = list.map((p) => ({ ...p }));
  },
  { immediate: true, deep: true }
);

const savePosition = async (pos: AdminPosition) => {
  savingCode.value = pos.positionCode;
  errorMsg.value = '';
  successMsg.value = '';
  try {
    await accessControlApi.upsertPosition({
      positionCode: pos.positionCode,
      positionName: pos.positionName,
      rankOrder: pos.rankOrder,
      defaultClearanceLevel: pos.defaultClearanceLevel,
      minAllowedClearance: pos.minAllowedClearance,
      maxAllowedClearance: pos.maxAllowedClearance,
      description: pos.description || undefined,
      reason: `治理中心调整职级 ${pos.positionCode} 默认密级为 L${pos.defaultClearanceLevel}`,
    });
    successMsg.value = `职级 ${pos.positionName} 已更新为默认 L${pos.defaultClearanceLevel}，并已广播全租户策略版本更新。`;
    emit('changed');
  } catch (err: any) {
    errorMsg.value = err.message || '更新职级密级失败';
  } finally {
    savingCode.value = '';
  }
};
</script>
