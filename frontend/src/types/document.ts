export type PipelineStepStatus = 'wait' | 'process' | 'finish' | 'error' | 'warning';

export interface PipelineStep {
  index: number;
  name: string;
  code: string;
  status: PipelineStepStatus;
  detail?: string;
  duration_ms?: number;
}

export const DEFAULT_PIPELINE_STEPS: PipelineStep[] = [
  { index: 1, name: '接入', code: 'ingest', status: 'wait' },
  { index: 2, name: 'MinerU 解析', code: 'mineru_parse', status: 'wait' },
  { index: 3, name: 'MD 加载', code: 'md_load', status: 'wait' },
  { index: 4, name: '图片上传', code: 'image_upload', status: 'wait' },
  { index: 5, name: '图片描述', code: 'image_vlm', status: 'wait' },
  { index: 6, name: '表格转文本', code: 'table_to_text', status: 'wait' },
  { index: 7, name: '人工审核', code: 'human_review', status: 'wait' },
  { index: 8, name: '表格回填', code: 'table_backfill', status: 'wait' },
  { index: 9, name: '切分', code: 'chunking', status: 'wait' },
  { index: 10, name: '向量化', code: 'vectorize', status: 'wait' },
  { index: 11, name: '入库', code: 'index_store', status: 'wait' }
];

export interface DepartmentItem {
  id: number;
  name: string;
  path: string;
}

export interface DocumentItem {
  doc_id: string;
  file_name: string;
  department: string;
  category: string;
  tags: string[];
  status: string;
  step_index: number;
  created_at: string;
  updated_at: string;
}

export interface ChunkItem {
  id?: string;
  document_id: string;
  chunk_index: number;
  chunk_label: string;
  content: string;
  is_code: boolean;
  is_table: boolean;
  page_idx: number;
  display_page: number;
  breadcrumb: string[];
  asset_url?: string;
  table_id?: string;
  table_html?: string;
  sparse_token_count?: number;
}

export interface ReviewItem {
  item_id: string;
  type: string;
  page_idx: number;
  display_page?: number;
  line_number?: number;
  vlm_description: string;
  user_description?: string;
  remark?: string;
  status?: string;
  html_table?: string;
  image_url?: string;
  asset_url?: string;
  asset_proxy_url?: string;
  raw_oss_url?: string;
  raw_content?: string;
  language?: string;
  title?: string;
  location_info?: string;
}

export interface PipelineSnapshot {
  doc_id: string;
  file_name: string;
  department: string;
  category: string;
  tags: string[];
  overall_status: string;
  current_step_index: number;
  steps: PipelineStep[];
  requires_review: boolean;
  pending_reviews: ReviewItem[];
  is_duplicate_warning?: boolean;
  matched_doc_id?: string;
  error?: string;
}
