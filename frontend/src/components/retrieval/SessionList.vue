<template>
  <div class="session-list-sidebar w-64 border-r border-slate-200/90 flex flex-col h-full bg-slate-50/70 backdrop-blur-md select-none">
    <!-- 顶部新会话按钮 (方案 D 皇家蔚蓝按钮) -->
    <div class="p-4 pb-3 border-b border-slate-200/80">
      <button 
        @click="handleNewSession"
        class="w-full py-2.5 px-4 bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold rounded-xl shadow-sm shadow-blue-500/20 flex items-center justify-center gap-2 transition-all group"
      >
        <svg class="w-4 h-4 group-hover:rotate-90 transition-transform" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        <span>+ 新建问答会话</span>
      </button>
    </div>

    <!-- 历史会话卡片列表 -->
    <div class="flex-1 overflow-y-auto p-2.5 space-y-1.5">
      <div 
        v-for="session in chatStore.sessions" 
        :key="session.id"
        @click="chatStore.switchSession(session.id)"
        class="group relative p-3 rounded-xl cursor-pointer border transition-all"
        :class="[
          chatStore.currentSessionId === session.id
            ? 'bg-white border-blue-500 shadow-sm text-slate-900'
            : 'border-transparent hover:bg-white/80 hover:border-slate-200 text-slate-600'
        ]"
      >
        <!-- 会话标题 -->
        <div class="flex items-center justify-between">
          <div 
            class="text-xs font-medium truncate pr-4 transition-colors"
            :class="chatStore.currentSessionId === session.id ? 'text-slate-900 font-semibold' : 'text-slate-700 group-hover:text-slate-900'"
          >
            {{ session.title }}
          </div>

          <!-- 删除会话按钮 -->
          <button 
            @click.stop="handleDeleteSession(session.id)"
            class="opacity-0 group-hover:opacity-100 text-slate-400 hover:text-rose-600 transition-opacity p-0.5"
            title="删除会话"
          >
            <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
          </button>
        </div>

        <!-- 会话副标：轮数与时间戳 -->
        <div class="flex items-center space-x-2 text-[10px] text-slate-400 font-mono mt-1.5">
          <span>{{ session.messages.length }} 条问答</span>
          <span>·</span>
          <span>{{ formatTime(session.updatedAt) }}</span>
        </div>
      </div>
    </div>

    <!-- 底部状态指示 -->
    <div class="p-3 border-t border-slate-200/80 text-[11px] text-slate-400 flex items-center justify-between">
      <span>会话已自动云端同步</span>
      <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useChatStore } from '../../stores/chat';

const chatStore = useChatStore();

const handleNewSession = () => {
  chatStore.createNewSession();
};

const handleDeleteSession = (id: string) => {
  if (confirm('确认删除此问答会话及其历史记录吗？')) {
    chatStore.deleteSession(id);
  }
};

const formatTime = (ts: number) => {
  const d = new Date(ts);
  const m = (d.getMonth() + 1).toString().padStart(2, '0');
  const day = d.getDate().toString().padStart(2, '0');
  const h = d.getHours().toString().padStart(2, '0');
  const min = d.getMinutes().toString().padStart(2, '0');
  return `${m}/${day} ${h}:${min}`;
};
</script>
