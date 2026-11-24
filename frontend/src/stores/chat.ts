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
  let sessionListSeq = 0;
  let messageLoadSeq = 0;
  let activeGeneratingSessionId = '';

  const currentSession = computed(() => {
    return sessions.value.find((s) => s.id === currentSessionId.value) || sessions.value[0];
  });

  /**
   * 始终从服务端重新拉取经当前 READ 权限逐条复核后的会话历史，
   * 严禁将前端内存旧缓存当成服务端可信历史，确保权限回收后历史机密回答立刻消失。
   */
  const loadSessionMessages = async (sessionId: string) => {
    if (!sessionId) return;
    if (isGenerating.value && activeGeneratingSessionId === sessionId) {
      return;
    }

    const reqSeq = ++messageLoadSeq;
    isLoadingMessages.value = true;
    try {
      const msgs = await sessionsApi.getSessionMessages(sessionId, 100);
      // 若期间发生新建会话、切换会话或该会话已进入流式生成，立即丢弃过期结果防止覆盖界面
      if (
        reqSeq !== messageLoadSeq ||
        currentSessionId.value !== sessionId ||
        (isGenerating.value && activeGeneratingSessionId === sessionId)
      ) {
        return;
      }
      const target = sessions.value.find((s) => s.id === sessionId);
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
      if (reqSeq !== messageLoadSeq) {
        return;
      }
      console.error(`加载会话 ${sessionId} 消息失败:`, e);
      throw e;
    } finally {
      if (reqSeq === messageLoadSeq) {
        isLoadingMessages.value = false;
      }
    }
  };

  /** 初始化加载云端会话列表，并始终向服务端重载当前会话已复核历史 */
  const initSessions = async (force: boolean = true) => {
    if (!force && sessions.value.length > 0 && currentSessionId.value) {
      await loadSessionMessages(currentSessionId.value);
      return;
    }

    const reqSeq = ++sessionListSeq;
    isLoadingSessions.value = true;
    try {
      const list = await sessionsApi.listSessions(50, 0);
      if (list && list.length > 0) {
        const existingById = new Map(sessions.value.map((s) => [s.id, s]));
        const mergedList: ChatSession[] = list.map((item) => {
          const prev = existingById.get(item.id);
          if (prev && (prev.id === activeGeneratingSessionId || (reqSeq !== sessionListSeq && prev.id === currentSessionId.value))) {
            // 保留正在流式生成或期间新建的响应式会话对象，避免 sendMessage 闭包脱离数组
            prev.title = prev.title || item.title;
            return prev;
          }
          return {
            id: item.id,
            title: item.title,
            messageCount: item.message_count ?? 0,
            updatedAt: new Date(item.updated_at || item.created_at).getTime(),
            messages: prev ? prev.messages : [],
            scene_type: prev?.scene_type || 'general',
            enable_hyde: prev?.enable_hyde ?? false,
          };
        });

        // 若期间前端刚刚创建了新会话但尚未出现在本次旧 list 响应中，保留在列表头部
        for (const localSess of sessions.value) {
          if (!mergedList.some((m) => m.id === localSess.id)) {
            mergedList.unshift(localSess);
          }
        }
        sessions.value = mergedList;

        // 若加载期间用户已经点击了「+ 新建」或切换了会话，绝不回退覆盖 currentSessionId 或拉旧历史
        if (reqSeq !== sessionListSeq || isGenerating.value) {
          return;
        }

        const lastActiveId = localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY);
        const matched = sessions.value.find((s) => s.id === lastActiveId);
        const targetId = matched ? matched.id : sessions.value[0].id;
        currentSessionId.value = targetId;
        localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, targetId);

        await loadSessionMessages(targetId);
      } else {
        if (reqSeq !== sessionListSeq || sessions.value.length > 0 || isGenerating.value) {
          return;
        }
        const created = await sessionsApi.createSession('新会话');
        if (reqSeq !== sessionListSeq) {
          return;
        }
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
      if (reqSeq === sessionListSeq) {
        isLoadingSessions.value = false;
      }
    }
  };

  const createNewSession = async (title: string = '新会话'): Promise<string> => {
    // 立即作废在途的会话列表初始化与历史消息加载，复位 loading 状态
    sessionListSeq++;
    messageLoadSeq++;
    isLoadingMessages.value = false;
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
    if (!sessions.value.some((s) => s.id === created.id)) {
      sessions.value.unshift(newSession);
    }
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
    const liveTarget = sessions.value.find((s) => s.id === id) || target;
    if (liveTarget) {
      liveTarget.title = updated.title || trimmed;
      liveTarget.updatedAt = Date.now();
    }
  };

  const switchSession = async (id: string) => {
    sessionListSeq++;
    messageLoadSeq++;
    isLoadingMessages.value = false;
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
    activeGeneratingSessionId = '';
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

    // 取消任何在途的旧会话初始化/历史加载，防止竞态覆盖新消息
    sessionListSeq++;
    messageLoadSeq++;
    isLoadingMessages.value = false;

    if (!currentSession.value) {
      await createNewSession();
    }
    const initialSession = currentSession.value;
    if (!initialSession) return;
    const sessionId = initialSession.id;

    const getLiveSession = () => sessions.value.find((s) => s.id === sessionId) || initialSession;
    const session = getLiveSession();

    const priorUserMessages = session.messages.filter((m) => m.role === 'user');
    if (priorUserMessages.length === 0) {
      const derivedTitle = query.length > 22 ? query.slice(0, 22) + '...' : query;
      session.title = derivedTitle;
      sessionsApi.updateSession(sessionId, derivedTitle).catch((err) => {
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
    activeGeneratingSessionId = sessionId;
    abortController = new AbortController();

    const getLiveAssistant = () => {
      const liveSess = getLiveSession();
      const found = liveSess.messages.find((m) => m.id === assistantMsgId);
      if (found) return found;
      // 极端情况保护：若消息意外缺失，重新挂载到活跃会话末尾确保界面可见
      liveSess.messages.push(assistantMessage);
      return assistantMessage;
    };

    try {
      await retrievalApi.streamChat(
        {
          query,
          conversation_id: sessionId,
          kb_ids: options?.kb_ids && options.kb_ids.length > 0 ? options.kb_ids : undefined,
          category_scope: options?.category_scope,
          tags_scope: options?.tags_scope,
          scene_type: options?.scene_type || session.scene_type || 'general',
          enable_hyde: options?.enable_hyde !== undefined ? options.enable_hyde : session.enable_hyde ?? true,
        },
        {
          onThinking: (thinking: ThinkingProcess) => {
            getLiveAssistant().thinking = thinking;
          },
          onCitations: (citations: CitationSource[]) => {
            getLiveAssistant().citations = citations;
          },
          onReasoning: (token: string) => {
            const t = getLiveAssistant();
            t.reasoning_content = (t.reasoning_content || '') + token;
            t.isReasoning = true;
          },
          onMessage: (token: string) => {
            const t = getLiveAssistant();
            t.isReasoning = false;
            t.content += token;
          },
          onDone: (meta) => {
            const liveSess = getLiveSession();
            const t = getLiveAssistant();
            t.isStreaming = false;
            t.isReasoning = false;
            if (t.thinking) {
              t.thinking.total_overall_ms = meta.total_overall_ms;
            }
            liveSess.messageCount = liveSess.messages.length;
            liveSess.updatedAt = Date.now();
            isGenerating.value = false;
            activeGeneratingSessionId = '';
          },
          onError: (err: Error) => {
            console.error('[Chat Error]', err);
            const t = getLiveAssistant();
            t.isStreaming = false;
            t.isReasoning = false;
            t.error = err.message;
            isGenerating.value = false;
            activeGeneratingSessionId = '';
          },
        },
        abortController.signal
      );
    } catch (e: any) {
      const t = getLiveAssistant();
      t.isStreaming = false;
      t.isReasoning = false;
      t.error = e.message || '请求发生异常';
      isGenerating.value = false;
      activeGeneratingSessionId = '';
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
