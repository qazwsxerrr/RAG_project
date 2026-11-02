import { defineStore } from 'pinia';
import { ref, computed, reactive } from 'vue';
import type { ChatSession, ChatMessage, ThinkingProcess, CitationSource } from '../types';
import { retrievalApi } from '../api/retrieval';
import { sessionsApi } from '../api/sessions';
import { useAuthStore } from './auth';

const ACTIVE_SESSION_STORAGE_KEY = 'rag_active_session_id';

export const useChatStore = defineStore('chat', () => {
  const sessions = ref<ChatSession[]>([]);
  const currentSessionId = ref<string>('');
  const isLoadingSessions = ref<boolean>(false);
  const isLoadingMessages = ref<boolean>(false);
  const isGenerating = ref<boolean>(false);
  const loadedMessagesMap = reactive<Record<string, boolean>>({});
  let abortController: AbortController | null = null;

  const currentSession = computed(() => {
    return sessions.value.find((s) => s.id === currentSessionId.value) || sessions.value[0];
  });

  /** 加载指定会话的云端历史消息 */
  const loadSessionMessages = async (sessionId: string, force: boolean = false) => {
    if (!sessionId) return;
    const target = sessions.value.find((s) => s.id === sessionId);
    // 缓存防御：只有在非强制、已标记加载、且内存中确实已持有消息时才跳过，杜绝空数组死锁
    if (!force && loadedMessagesMap[sessionId] && target && target.messages.length > 0) return;

    isLoadingMessages.value = true;
    try {
      const msgs = await sessionsApi.getSessionMessages(sessionId, 100);
      if (target) {
        target.messages = msgs.map((m) => ({
          id: m.id,
          role: m.role,
          content: m.content,
          citations: m.citations || [],
          reasoning_content: m.reasoning_content || undefined,
          thinking: m.thinking_process || undefined,
          timestamp: new Date(m.created_at).getTime(),
        }));
        target.messageCount = target.messages.length;
        loadedMessagesMap[sessionId] = true;
      }
    } catch (e) {
      console.error(`加载会话 ${sessionId} 消息失败:`, e);
      throw e;
    } finally {
      isLoadingMessages.value = false;
    }
  };

  /** 初始化加载云端会话列表（幂等保活与增量同步） */
  const initSessions = async (force: boolean = false) => {
    // 治理原则：单页应用路由切换时，若 Store 中已有会话列表且当前会话数据完整，直接复用避免清空闪烁
    if (!force && sessions.value.length > 0) {
      if (currentSessionId.value) {
        const active = sessions.value.find((s) => s.id === currentSessionId.value);
        if (!active || !active.messages || active.messages.length === 0) {
          await loadSessionMessages(currentSessionId.value);
        }
      }
      return;
    }

    isLoadingSessions.value = true;
    try {
      const list = await sessionsApi.listSessions('default_tenant', 50, 0);
      if (list && list.length > 0) {
        // 保留现有已加载的消息对象映射，杜绝粗暴清空现有对话树
        const existingMessagesMap = new Map(sessions.value.map((s) => [s.id, s.messages]));

        sessions.value = list.map((item) => ({
          id: item.id,
          title: item.title,
          messageCount: item.message_count ?? 0,
          updatedAt: new Date(item.updated_at || item.created_at).getTime(),
          messages: existingMessagesMap.get(item.id) || [],
          scene_type: 'general',
          enable_hyde: false,
        }));

        const lastActiveId = localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY);
        const matched = sessions.value.find((s) => s.id === lastActiveId);
        const targetId = matched ? matched.id : sessions.value[0].id;
        currentSessionId.value = targetId;
        localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, targetId);

        const current = sessions.value.find((s) => s.id === targetId);
        if (!current || !current.messages || current.messages.length === 0) {
          await loadSessionMessages(targetId, true);
        }
      } else {
        // 云端尚无历史会话，创建云端新会话
        const created = await sessionsApi.createSession('新会话', 'default_tenant');
        const newSession: ChatSession = {
          id: created.id,
          title: created.title || '新会话',
          messageCount: 0,
          updatedAt: new Date(created.updated_at || created.created_at).getTime(),
          messages: [],
          scene_type: 'general',
          enable_hyde: false,
        };
        sessions.value = [newSession];
        currentSessionId.value = created.id;
        loadedMessagesMap[created.id] = true;
        localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, created.id);
      }
    } catch (e) {
      console.error('初始化云端会话列表失败:', e);
      throw e;
    } finally {
      isLoadingSessions.value = false;
    }
  };

  /** 创建全新云端会话 */
  const createNewSession = async (title: string = '新知识问答会话'): Promise<string> => {
    if (isGenerating.value) {
      stopGeneration();
    }
    const created = await sessionsApi.createSession(title, 'default_tenant');
    const newSession: ChatSession = {
      id: created.id,
      title: created.title || title,
      messageCount: 0,
      updatedAt: new Date(created.updated_at || created.created_at).getTime(),
      messages: [],
      scene_type: 'general',
      enable_hyde: false,
    };
    sessions.value.unshift(newSession);
    currentSessionId.value = created.id;
    loadedMessagesMap[created.id] = true;
    localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, created.id);
    return created.id;
  };

  /** 切换会话 */
  const switchSession = async (id: string) => {
    if (isGenerating.value) {
      stopGeneration();
    }
    currentSessionId.value = id;
    localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, id);

    if (!loadedMessagesMap[id]) {
      await loadSessionMessages(id);
    }
  };

  /** 物理删除云端会话及其名下所有历史消息 */
  const deleteSession = async (id: string) => {
    if (isGenerating.value && currentSessionId.value === id) {
      stopGeneration();
    }
    await sessionsApi.deleteSession(id, 'default_tenant');
    const idx = sessions.value.findIndex((s) => s.id === id);
    if (idx !== -1) {
      sessions.value.splice(idx, 1);
      delete loadedMessagesMap[id];
      if (sessions.value.length === 0) {
        await createNewSession();
      } else if (currentSessionId.value === id) {
        const nextId = sessions.value[0].id;
        currentSessionId.value = nextId;
        localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, nextId);
        if (!loadedMessagesMap[nextId]) {
          await loadSessionMessages(nextId);
        }
      }
    }
  };

  /** 中止当前大模型流式生成 */
  const stopGeneration = () => {
    if (abortController) {
      abortController.abort();
      abortController = null;
    }
    isGenerating.value = false;
  };

  /** 清空当前会话：物理删除云端旧会话并新建全新会话 */
  const clearCurrentMessages = async () => {
    const activeId = currentSessionId.value;
    if (!activeId) return;
    await deleteSession(activeId);
  };

  /** 发送问题并处理全流程流式问答 */
  const sendMessage = async (
    query: string,
    options?: {
      department_scope?: string[];
      category_scope?: string;
      tags_scope?: string[];
      scene_type?: string;
      enable_hyde?: boolean;
    }
  ) => {
    if (!query.trim() || isGenerating.value) return;

    if (!currentSession.value) {
      await createNewSession();
    }
    const session = currentSession.value;
    if (!session) return;

    // 治理原则：基于客观事实判断。若当前会话此前尚无任何用户提问，自动生成标题并显式同步至后端 SSOT
    const priorUserMessages = session.messages.filter((m) => m.role === 'user');
    if (priorUserMessages.length === 0) {
      const derivedTitle = query.length > 22 ? query.slice(0, 22) + '...' : query;
      session.title = derivedTitle;
      // 显式调用 RESTful PATCH 契约持久化，后端在事务行锁中兜底防止并发覆盖
      sessionsApi.updateSession(session.id, derivedTitle).catch((err) => {
        console.warn('异步更新会话标题警告 (后端事务行锁将兜底保证一致性):', err);
      });
    }

    // 1. 压入用户提问
    const userMsgId = 'msg_u_' + Date.now();
    const userMessage: ChatMessage = {
      id: userMsgId,
      role: 'user',
      content: query,
      timestamp: Date.now(),
    };
    session.messages.push(userMessage);

    // 2. 压入 AI 响应占位卡片 (初始带有 isStreaming)
    const assistantMsgId = 'msg_a_' + Date.now();
    const assistantMessage = reactive<ChatMessage>({
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      isStreaming: true,
      timestamp: Date.now(),
    });
    session.messages.push(assistantMessage);

    session.messageCount = session.messages.length;
    session.updatedAt = Date.now();

    isGenerating.value = true;
    abortController = new AbortController();

    // 3. 构建历史消息上下文 (排除本次刚刚压入的用户提问与占位助手卡片，仅回溯已完成的前序问答)
    const historyPayload = session.messages
      .slice(0, -2)
      .filter((m) => m.content && !m.isStreaming && !m.error)
      .slice(-6)
      .map((m) => ({ role: m.role, content: m.content }));

    try {
      await retrievalApi.streamChat(
        {
          query,
          conversation_id: session.id,
          department_scope: options?.department_scope,
          category_scope: options?.category_scope,
          tags_scope: options?.tags_scope,
          scene_type: options?.scene_type || session.scene_type || 'general',
          enable_hyde: options?.enable_hyde !== undefined ? options.enable_hyde : session.enable_hyde ?? true,
          history: historyPayload,
        },
        {
          onThinking: (thinking: ThinkingProcess) => {
            const target = session.messages.find((m) => m.id === assistantMsgId);
            if (target) {
              target.thinking = thinking;
            } else {
              assistantMessage.thinking = thinking;
            }
          },
          onCitations: (citations: CitationSource[]) => {
            const target = session.messages.find((m) => m.id === assistantMsgId);
            if (target) {
              target.citations = citations;
            } else {
              assistantMessage.citations = citations;
            }
          },
          onReasoning: (token: string) => {
            const target = session.messages.find((m) => m.id === assistantMsgId);
            const t = target || assistantMessage;
            t.reasoning_content = (t.reasoning_content || '') + token;
            t.isReasoning = true;
          },
          onMessage: (token: string) => {
            const target = session.messages.find((m) => m.id === assistantMsgId);
            const t = target || assistantMessage;
            t.isReasoning = false;
            t.content += token;
          },
          onDone: (meta) => {
            const target = session.messages.find((m) => m.id === assistantMsgId);
            const t = target || assistantMessage;
            t.isStreaming = false;
            t.isReasoning = false;
            if (t.thinking) {
              t.thinking.total_overall_ms = meta.total_overall_ms;
            }
            session.messageCount = session.messages.length;
            session.updatedAt = Date.now();
            isGenerating.value = false;
          },
          onError: (err: Error) => {
            console.error('[Chat Error]', err);
            const target = session.messages.find((m) => m.id === assistantMsgId);
            const t = target || assistantMessage;
            t.isStreaming = false;
            t.isReasoning = false;
            t.error = err.message;
            isGenerating.value = false;

            if (err.message && (err.message.includes('越权') || err.message.includes('403'))) {
              const authStore = useAuthStore();
              authStore.interceptAlert = err.message;
            }
          },
        },
        abortController.signal
      );
    } catch (e: any) {
      assistantMessage.isStreaming = false;
      assistantMessage.error = e.message || '请求发生异常';
      isGenerating.value = false;

      if (e.message && (e.message.includes('越权') || e.message.includes('403'))) {
        const authStore = useAuthStore();
        authStore.interceptAlert = e.message;
      }
    }
  };

  return {
    sessions,
    currentSessionId,
    currentSession,
    isLoadingSessions,
    isLoadingMessages,
    isGenerating,
    initSessions,
    loadSessionMessages,
    createNewSession,
    switchSession,
    deleteSession,
    stopGeneration,
    clearCurrentMessages,
    sendMessage,
  };
});
