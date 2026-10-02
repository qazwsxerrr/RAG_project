import { defineStore } from 'pinia';
import { ref, computed, reactive } from 'vue';
import type { ChatSession, ChatMessage, ThinkingProcess, CitationSource } from '../types';
import { retrievalApi } from '../api/retrieval';

const STORAGE_KEY = 'rag_chat_sessions_v1';

export const useChatStore = defineStore('chat', () => {
  // 从 localStorage 恢复会话列表
  const loadInitialSessions = (): ChatSession[] => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed) && parsed.length > 0) return parsed;
      }
    } catch (e) {
      console.warn('恢复历史会话失败:', e);
    }
    const defaultId = 'session_' + Date.now();
    return [
      {
        id: defaultId,
        title: '新知识问答会话',
        messageCount: 0,
        updatedAt: Date.now(),
        messages: [],
        scene_type: 'general',
        enable_hyde: false,
      },
    ];
  };

  const sessions = ref<ChatSession[]>(loadInitialSessions());
  const currentSessionId = ref<string>(sessions.value[0]?.id || '');
  const isGenerating = ref<boolean>(false);
  let abortController: AbortController | null = null;

  // 持久化保存到 localStorage
  const persistSessions = () => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions.value));
    } catch (e) {
      console.warn('持久化会话失败:', e);
    }
  };

  const currentSession = computed(() => {
    return sessions.value.find((s) => s.id === currentSessionId.value) || sessions.value[0];
  });

  /** 创建全新会话 */
  const createNewSession = (): string => {
    const newId = 'session_' + Date.now();
    const newSession: ChatSession = {
      id: newId,
      title: '新知识问答会话',
      messageCount: 0,
      updatedAt: Date.now(),
      messages: [],
      scene_type: 'general',
      enable_hyde: false,
    };
    sessions.value.unshift(newSession);
    currentSessionId.value = newId;
    persistSessions();
    return newId;
  };

  /** 切换会话 */
  const switchSession = (id: string) => {
    if (isGenerating.value) {
      stopGeneration();
    }
    currentSessionId.value = id;
  };

  /** 删除会话 */
  const deleteSession = (id: string) => {
    const idx = sessions.value.findIndex((s) => s.id === id);
    if (idx !== -1) {
      sessions.value.splice(idx, 1);
      if (sessions.value.length === 0) {
        createNewSession();
      } else if (currentSessionId.value === id) {
        currentSessionId.value = sessions.value[0].id;
      }
      persistSessions();
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

  /** 清空当前会话消息 */
  const clearCurrentMessages = () => {
    if (currentSession.value) {
      currentSession.value.messages = [];
      currentSession.value.messageCount = 0;
      persistSessions();
    }
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

    const session = currentSession.value;
    if (!session) return;

    // 若为会话第一条提问，自动根据问题前 20 字更新会话标题
    if (session.messages.length === 0 || session.title === '新知识问答会话') {
      session.title = query.length > 22 ? query.slice(0, 22) + '...' : query;
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
    persistSessions();

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
            isGenerating.value = false;
            persistSessions();
          },
          onError: (err: Error) => {
            console.error('[Chat Error]', err);
            const target = session.messages.find((m) => m.id === assistantMsgId);
            const t = target || assistantMessage;
            t.isStreaming = false;
            t.isReasoning = false;
            t.error = err.message;
            isGenerating.value = false;
            persistSessions();
          },
        },
        abortController.signal
      );
    } catch (e: any) {
      assistantMessage.isStreaming = false;
      assistantMessage.error = e.message || '请求发生异常';
      isGenerating.value = false;
      persistSessions();
    }
  };

  const reloadSessions = () => {
    sessions.value = loadInitialSessions();
  };

  return {
    sessions,
    currentSessionId,
    currentSession,
    isGenerating,
    createNewSession,
    switchSession,
    deleteSession,
    stopGeneration,
    clearCurrentMessages,
    sendMessage,
    reloadSessions,
  };
});
