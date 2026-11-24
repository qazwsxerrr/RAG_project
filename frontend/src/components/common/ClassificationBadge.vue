<template>
  <span
    class="inline-flex items-center gap-1.5 font-medium select-none whitespace-nowrap"
    :class="[sizeClass, toneClass]"
    :title="fullLabel"
  >
    <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="dotClass"></span>
    <span>{{ compact ? shortText : fullLabel }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { CLASSIFICATION_META } from '../../types/authz';

const props = withDefaults(defineProps<{
  level?: number | null;
  compact?: boolean;
  size?: 'xs' | 'sm';
}>(), {
  level: 2,
  compact: false,
  size: 'xs',
});

const safeLevel = computed(() => {
  const n = Number(props.level ?? 2);
  if (n >= 1 && n <= 4) return n;
  return 2;
});

const meta = computed(() => CLASSIFICATION_META[safeLevel.value] || CLASSIFICATION_META[2]);
const fullLabel = computed(() => meta.value.label);
const shortText = computed(() => `${meta.value.code} ${meta.value.short}`);

const sizeClass = computed(() =>
  props.size === 'sm' ? 'text-xs' : 'text-[11px]'
);

const toneClass = computed(() => {
  switch (safeLevel.value) {
    case 1:
      return 'text-emerald-700';
    case 2:
      return 'text-sky-700';
    case 3:
      return 'text-amber-700';
    case 4:
      return 'text-rose-700';
    default:
      return 'text-slate-600';
  }
});

const dotClass = computed(() => {
  switch (safeLevel.value) {
    case 1:
      return 'bg-emerald-500';
    case 2:
      return 'bg-sky-500';
    case 3:
      return 'bg-amber-500';
    case 4:
      return 'bg-rose-600';
    default:
      return 'bg-slate-400';
  }
});
</script>
