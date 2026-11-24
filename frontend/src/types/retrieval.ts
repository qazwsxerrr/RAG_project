export interface ThinkingStep {
  name: string;
  status: string;
  duration_ms: number;
  count_change?: string;
  summary?: string;
}

export interface ThinkingProcess {
  total_retrieve_ms: number;
  total_overall_ms: number;
  steps: ThinkingStep[];
}

export interface CitationSource {
  citation_id: number;
  chunk_id: string;
  document_id: string;
  document_title: string;
  kb_id?: string;
  kb_name?: string;
  classification_level?: number;
  chunk_label: string;
  page_idx: number;
  display_page?: number;
  bbox?: number[];
  snippet: string;
  asset_url?: string;
  preview_url?: string;
  page_render_url?: string;
  asset_proxy_url?: string;
  total_pages?: number;
  is_pdf?: boolean;
  score?: number;
  is_table?: boolean;
  table_html?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  reasoning_content?: string;
  isReasoning?: boolean;
  reasoning_ms?: number;
  thinking?: ThinkingProcess;
  citations?: CitationSource[];
  timestamp: number;
  isStreaming?: boolean;
  error?: string;
}

export interface ChatSession {
  id: string;
  title: string;
  messageCount: number;
  updatedAt: number;
  messages: ChatMessage[];
  scene_type?: string;
  enable_hyde?: boolean;
  kb_ids?: string[];
}
