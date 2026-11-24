import { defineStore } from 'pinia';
import { ref, computed, reactive } from 'vue';
import type { ChatSession, ChatMessage, ThinkingProcess, CitationSource } from '../types';
import { retrievalApi } from '../api/retrieval';
import { sessionsApi } from '../api/sessions';

const ACTIVE_SESSION_STORAGE_KEY = 'rag_active_session_id';

export const useChatStore = defineStore('chat', () => {
  const sessions = ref<ChatSession[]>([]);
  const currentSessionId = ref<string>('');
  const isLoadingSessions = ref<boolean>(false);
  const isLoadingMessages = ref<boolean>(false);
  const isGenerating = ref<boolean>(false);
  let abortController: AbortController | null = null;

  const currentSession = computed(() => {
    return sessions.value.find((s) => s.id === currentSessionId.value) || sessions.value[0];
  });

  /**
   * 始终从服务端重新拉取经当前 READ 权限逐条复核后的会话历史，
   * 严禁将前端内存旧缓存当成服务端可信历史，确保权限回收后历史机密回答立刻消失。
   */
  const loadSessionMessages = async (sessionId: string) => {
    if (!sessionId) return;
    const target = sessions.value.find((s) => s.id === sessionId);

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
      }
    } catch (e) {
      console.error(`加载会话 ${sessionId} 消息失败:`, e);
      throw e;
    } finally {
      isLoadingMessages.value = false;
    }
  };

  /** 初始化加载云端会话列表，并始终向服务端重载当前会话已复核历史 */
  const initSessions = async (force: boolean = true) => {
    if (!force && sessions.value.length > 0 && currentSessionId.value) {
      await loadSessionMessages(currentSessionId.value);
      return;
    }

    isLoadingSessions.value = true;
    try {
      const list = await sessionsApi.listSessions(50, 0);
      if (list && list.length > 0) {
        sessions.value = list.map((item) => ({
          id: item.id,
          title: item.title,
          messageCount: item.message_count ?? 0,
          updatedAt: new Date(item.updated_at || item.created_at).getTime(),
          messages: [],
          scene_type: 'general',
          enable_hyde: false,
        }));

        const lastActiveId = localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY);
        const matched = sessions.value.find((s) => s.id === lastActiveId);
        const targetId = matched ? matched.id : sessions.value[0].id;
        currentSessionId.value = targetId;
        localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, targetId);

        await loadSessionMessages(targetId);
      } else {
        const created = await sessionsApi.createSession('新会话');
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
        localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, created.id);
      }
    } catch (e) {
      console.error('初始化云端会话列表失败:', e);
      throw e;
    } finally {
      isLoadingSessions.value = false;
    }
  };

  const createNewSession = async (title: string = '新会话'): Promise<string> => {
    if (isGenerating.value) {
      stopGeneration();
    }
    const created = await sessionsApi.createSession(title);
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
    localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, created.id);
    return created.id;
  };

  const renameSession = async (id: string, newTitle: string) => {
    const trimmed = newTitle.trim();
    if (!trimmed) return;
    const target = sessions.value.find((s) => s.id === id);
    if (target && target.title === trimmed) return;
    const updated = await sessionsApi.updateSession(id, trimmed);
    if (target) {
      target.title = updated.title || trimmed;
      target.updatedAt = Date.now();
    }
  };

  const switchSession = async (id: string) => {
    if (isGenerating.value) {
      stopGeneration();
    }
    currentSessionId.value = id;
    localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, id);
    await loadSessionMessages(id);
  };

  const deleteSession = async (id: string) => {
    if (isGenerating.value && currentSessionId.value === id) {
      stopGeneration();
    }
    await sessionsApi.deleteSession(id);
    const idx = sessions.value.findIndex((s) => s.id === id);
    if (idx !== -1) {
      sessions.value.splice(idx, 1);
      if (sessions.value.length === 0) {
        await createNewSession();
      } else if (currentSessionId.value === id) {
        const nextId = sessions.value[0].id;
        currentSessionId.value = nextId;
        localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, nextId);
        await loadSessionMessages(nextId);
      }
    }
  };

  const stopGeneration = () => {
    if (abortController) {
      abortController.abort();
      abortController = null;
    }
    isGenerating.value = false;
  };

  const clearCurrentMessages = async () => {
    const activeId = currentSessionId.value;
    if (!activeId) return;
    await deleteSession(activeId);
  };

  const sendMessage = async (
    query: string,
    options?: {
      kb_ids?: string[];
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

    const priorUserMessages = session.messages.filter((m) => m.role === 'user');
    if (priorUserMessages.length === 0) {
      const derivedTitle = query.length > 22 ? query.slice(0, 22) + '...' : query;
      session.title = derivedTitle;
      sessionsApi.updateSession(session.id, derivedTitle).catch((err) => {
        console.warn('异步更新会话标题警告:', err);
      });
    }

    const userMsgId = 'msg_u_' + Date.now();
    session.messages.push({
      id: userMsgId,
      role: 'user',
      content: query,
      timestamp: Date.now(),
    });

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

    try {
      await retrievalApi.streamChat(
        {
          query,
          conversation_id: session.id,
          kb_ids: options?.kb_ids && options.kb_ids.length > 0 ? options.kb_ids : undefined,
          category_scope: options?.category_scope,
          tags_scope: options?.tags_scope,
          scene_type: options?.scene_type || session.scene_type || 'general',
          enable_hyde: options?.enable_hyde !== undefined ? options.enable_hyde : session.enable_hyde ?? true,
        },
        {
          onThinking: (thinking: ThinkingProcess) => {
            const target = session.messages.find((m) => m.id === assistantMsgId);
            (target || assistantMessage).thinking = thinking;
          },
          onCitations: (citations: CitationSource[]) => {
            const target = session.messages.find((m) => m.id === assistantMsgId);
            (target || assistantMessage).citations = citations;
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
          },
        },
        abortController.signal
      );
    } catch (e: any) {
      assistantMessage.isStreaming = false;
      assistantMessage.error = e.message || '请求发生异常';
      isGenerating.value = false;
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
    renameSession,
    switchSession,
    deleteSession,
    stopGeneration,
    clearCurrentMessages,
    sendMessage,
  };
});
