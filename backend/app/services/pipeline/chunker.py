import re
import uuid
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field, field_validator

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

logger = logging.getLogger(__name__)

class RawChunkData(BaseModel):
    """未向量化的结构化切片数据模型"""
    chunk_index: int = 0
    chunk_label: str = ""
    content: str
    is_code: bool = False
    is_table: bool = False
    table_html: Optional[str] = None
    page_idx: int = 0
    bbox: List[int] = Field(default_factory=list)
    breadcrumb: List[str] = Field(default_factory=list)
    asset_url: Optional[str] = None
    table_id: Optional[str] = None

    @field_validator("bbox", mode="before")
    @classmethod
    def validate_bbox(cls, v):
        if v is None:
            return []
        return list(v)


class DocumentChunker:
    """企业级 Markdown 混合切分引擎 (Phase 2 - 节点 9)

    核心规范:
    1. 原位保护: 提取图片/表格回填块与代码/Mermaid图，并在原文中替换为占位符，严禁追加到文末或割裂上下文；
    2. 正文两阶段切分: MarkdownHeaderTextSplitter 分组提取 breadcrumb，RecursiveCharacterTextSplitter 控长 800 字符；
    3. 图表原子切片: 每个回填块独立成片，剥离 HTML 与注释，表格以 Phase 1 回填单元为单位不拆不合；
    4. 严格 Fail-Fast: 内容为空或超出 8192 Token 限制时立即抛错，杜绝隐式截断与伪造兜底。
    """

    def __init__(
        self,
        chunk_size: int = 800,
        chunk_overlap: int = 100,
        max_embedding_chars: int = 8000
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_embedding_chars = max_embedding_chars

        # 正文两阶段切分器初始化 (必须使用 LangChain 原生类，严禁正则切分替代)
        self.header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[
                ("#", "h1"),
                ("##", "h2"),
                ("###", "h3"),
                ("####", "h4"),
                ("#####", "h5"),
                ("######", "h6"),
            ],
            strip_headers=True,
        )

        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            separators=["\n\n", "\n", "。", "！", "？", "；", " ", ""]
        )

    def split_enhanced_markdown(
        self,
        markdown_text: str,
        department: str = "总公司",
        content_list: Optional[List[Dict[str, Any]]] = None
    ) -> List[RawChunkData]:
        """对 full.enhanced.md 执行完整切分流程并返回结构化切片列表"""
        if not markdown_text or not markdown_text.strip():
            raise RuntimeError("【切分异常】输入 Markdown 内容为空，流水线立即终止！")

        blocks_map: Dict[str, Dict[str, Any]] = {}

        # -------------------------------------------------------------
        # 1. 扫描并隔离图表回填块 (rag-multimodal-block)
        # -------------------------------------------------------------
        div_pattern = re.compile(
            r'<div\s+([^>]*class="[^"]*rag-multimodal-block[^"]*"[^>]*)>(.*?)</div>',
            re.DOTALL
        )

        def _sub_multimodal(match: re.Match) -> str:
            attrs = match.group(1)
            inner = match.group(2)
            token = f"__MULTIMODAL_ASSET_TOKEN_{uuid.uuid4().hex}__"

            # 提取属性
            t_match = re.search(r'data-type="([^"]+)"', attrs)
            b_type = t_match.group(1) if t_match else "image"
            p_match = re.search(r'data-page="([^"]+)"', attrs)
            page_val = int(p_match.group(1)) if p_match else 1
            a_match = re.search(r'data-asset="([^"]+)"', attrs)
            asset_url = a_match.group(1).strip() if a_match else None
            tbl_match = re.search(r'data-table-id="([^"]+)"', attrs)
            table_id = tbl_match.group(1) if tbl_match else None
            lang_match = re.search(r'data-language="([^"]+)"', attrs)
            lang = lang_match.group(1).strip() if lang_match else None

            # 提取核验描述 (VLM_VERIFIED_DESCRIPTION_START ~ END)
            desc_match = re.search(
                r'<!--\s*VLM_VERIFIED_DESCRIPTION_START\s*-->\s*(.*?)\s*<!--\s*VLM_VERIFIED_DESCRIPTION_END\s*-->',
                inner,
                re.DOTALL
            )
            clean_desc = desc_match.group(1).strip() if desc_match else ""

            if not clean_desc:
                raise RuntimeError(
                    f"【切分红线阻断】检测到图表回填描述为空 [data-type={b_type}, data-page={page_val}]！"
                    "根据 AGENTS.md 准则，严禁以伪造套话填充，请排查 Phase 1 VLM 回填或审核数据。"
                )

            # 提取原始表格 HTML (若存在)
            tbl_html = None
            if b_type == "table":
                raw_tbl_match = re.search(r'(<table[\s\S]*?</table>)', inner, re.IGNORECASE)
                if raw_tbl_match:
                    tbl_html = raw_tbl_match.group(1).strip()

            final_content = clean_desc
            # 代码块与流程图：检索正文坚决只保留验证后的 AI 总结，避免源码符号污染向量检索
            # 同时将原始代码/Mermaid格式化存入 table_html 富文本呈现字段，满足展示需要
            if b_type in ("code", "flowchart"):
                raw_code_match = re.search(r'<!--\s*RAW_CODE_START\s*-->\s*([\s\S]*?)\s*<!--\s*RAW_CODE_END\s*-->', inner)
                raw_code_str = raw_code_match.group(1).strip() if raw_code_match else ""

                if raw_code_str:
                    import html
                    c_lang = lang or ("mermaid" if b_type == "flowchart" else "text")
                    escaped_code = html.escape(raw_code_str)
                    tbl_html = f'<pre><code class="language-{c_lang}">{escaped_code}</code></pre>'

                final_content = clean_desc

            blocks_map[token] = {
                "type": b_type,
                "language": lang,
                "page_idx": max(0, page_val - 1),
                "asset_url": asset_url if asset_url else None,
                "table_id": table_id,
                "content": final_content,
                "is_table": (b_type == "table"),
                "is_code": (b_type in ["code", "flowchart"]),
                "table_html": tbl_html,
                "bbox": []
            }
            return f"\n\n{token}\n\n"

        text_with_asset_tokens = div_pattern.sub(_sub_multimodal, markdown_text)

        # -------------------------------------------------------------
        # 2. 第一阶段: MarkdownHeaderTextSplitter 分章节并提取标题面包屑
        # -------------------------------------------------------------
        header_sections = self.header_splitter.split_text(text_with_asset_tokens)

        # 辅助快速按文本匹配原始 content_list 中的物理页码与包围盒坐标
        def _resolve_text_geometry(snippet: str) -> tuple[int, list]:
            if not content_list:
                return 0, []
            clean_sub = snippet[:60].strip()
            if not clean_sub:
                return 0, []

            # 1. 过滤掉非文本图元（图片、表格）及空文本，执行高精度子串匹配
            for item in content_list:
                it_type = item.get("type", "")
                if it_type in ["image", "table"]:
                    continue
                it_text = str(item.get("text", "")).strip()
                if not it_text or len(it_text) < 4:
                    continue
                if clean_sub in it_text or (len(it_text) >= 10 and it_text in clean_sub):
                    return item.get("page_idx", 0), (item.get("bbox") or [])

            # 2. 规范化标点符号与空白后的模糊语义匹配
            norm_sub = re.sub(r'[\s\W_]+', '', clean_sub)
            if len(norm_sub) >= 6:
                for item in content_list:
                    if item.get("type") in ["image", "table"]:
                        continue
                    it_text = str(item.get("text", "")).strip()
                    norm_it = re.sub(r'[\s\W_]+', '', it_text)
                    if not norm_it or len(norm_it) < 4:
                        continue
                    if norm_sub[:16] in norm_it or (len(norm_it) >= 10 and norm_it in norm_sub):
                        return item.get("page_idx", 0), (item.get("bbox") or [])

            return 0, []

        token_split_regex = re.compile(r'(__MULTIMODAL_ASSET_TOKEN_[a-f0-9]{32}__)')
        chunks: List[RawChunkData] = []
        global_chunk_idx = 0
        current_active_page = 0

        for section in header_sections:
            # 组装 breadcrumb 标题路径
            breadcrumb: List[str] = [
                section.metadata[h].strip()
                for h in ["h1", "h2", "h3", "h4", "h5", "h6"]
                if h in section.metadata and section.metadata[h].strip()
            ]

            sec_text = section.page_content.strip()
            if not sec_text:
                continue

            # 拆分出占位符与普通正文片段
            segments = token_split_regex.split(sec_text)

            for seg in segments:
                seg_clean = seg.strip()
                if not seg_clean:
                    continue

                if seg_clean in blocks_map:
                    # 完整块 (图片 / 表格 / 代码) 独立成片
                    blk = blocks_map[seg_clean]
                    if blk["page_idx"] == 0 and current_active_page > 0:
                        blk["page_idx"] = current_active_page
                    else:
                        current_active_page = blk["page_idx"]

                    # 长度上限校验 (Fail-Fast)
                    if len(blk["content"]) > self.max_embedding_chars:
                        raise RuntimeError(
                            f"【切片超限红线】图表或代码块长度超出模型上限 ({len(blk['content'])} > {self.max_embedding_chars})，"
                            f"严禁隐式静默截断！请人工核验或调整模型上下文。"
                        )

                    c_label = ""
                    if blk["is_table"]:
                        t_suffix = f"表格 {blk['table_id']}" if blk.get("table_id") else f"表格 (第 {blk['page_idx'] + 1} 页)"
                        c_label = f"{department} · {t_suffix}"
                    elif blk["type"] == "image":
                        c_label = f"{department} · 配图 (第 {blk['page_idx'] + 1} 页)"
                    elif blk["type"] == "flowchart":
                        c_label = f"{department} · 流程图 (第 {blk['page_idx'] + 1} 页)"
                    elif blk["type"] == "code":
                        lang_tag = f" ({blk.get('language')})" if blk.get('language') else ""
                        c_label = f"{department} · 代码块{lang_tag} (第 {blk['page_idx'] + 1} 页)"
                    elif blk["is_code"]:
                        c_label = f"{department} · 代码/流程图 (第 {global_chunk_idx + 1} 片)"
                    else:
                        c_label = f"{department} · 第 {global_chunk_idx + 1} 片"

                    chunks.append(RawChunkData(
                        chunk_index=global_chunk_idx,
                        chunk_label=c_label,
                        content=blk["content"],
                        is_code=blk["is_code"],
                        is_table=blk["is_table"],
                        table_html=blk["table_html"],
                        page_idx=blk["page_idx"],
                        bbox=blk["bbox"],
                        breadcrumb=list(breadcrumb),
                        asset_url=blk["asset_url"],
                        table_id=blk.get("table_id")
                    ))
                    global_chunk_idx += 1
                else:
                    # -------------------------------------------------------------
                    # 4. 第二阶段: RecursiveCharacterTextSplitter 普通正文长度控制
                    # -------------------------------------------------------------
                    plain_chunks = self.text_splitter.split_text(seg_clean)
                    for p_chunk in plain_chunks:
                        p_str = p_chunk.strip()
                        if not p_str:
                            continue

                        # 过滤掉纯标点符号碎片（如 ---, ***, ===, ###）与无意义碎片，杜绝 Step 11 稀疏向量字典为空崩溃
                        clean_alnum = re.sub(r'[\s\-_*#=>|`]+', '', p_str)
                        if len(clean_alnum) < 5:
                            logger.info(f"【切片过滤】剔除纯语法/标点碎片: {repr(p_str)}")
                            continue

                        # 解析几何坐标与页码
                        p_page, p_bbox = _resolve_text_geometry(p_str)
                        if p_page == 0 and not p_bbox and current_active_page > 0:
                            p_page = current_active_page
                        elif p_page > 0:
                            current_active_page = p_page

                        c_label = f"{department} · 第 {global_chunk_idx + 1} 片"
                        chunks.append(RawChunkData(
                            chunk_index=global_chunk_idx,
                            chunk_label=c_label,
                            content=p_str,
                            is_code=False,
                            is_table=False,
                            table_html=None,
                            page_idx=p_page,
                            bbox=p_bbox,
                            breadcrumb=list(breadcrumb),
                            asset_url=None,
                            table_id=None
                        ))
                        global_chunk_idx += 1

        # -------------------------------------------------------------
        # 5. 全量自检与断言 (Fail-Fast)
        # -------------------------------------------------------------
        if not chunks:
            raise RuntimeError("【切分异常】全文档切分后未产出任何有效切片，流程终止！")

        for idx, c in enumerate(chunks):
            if not c.content.strip():
                raise RuntimeError(f"【切片红线】第 {idx} 个切片内容为空！严禁空切片进入向量化！")
            if c.chunk_index != idx:
                raise RuntimeError(f"【切片顺序异常】切片编号不连续: 期望 {idx}，实际为 {c.chunk_index}！")

        logger.info(f"【切分完成】共生成 {len(chunks)} 个切片 (图表块原子独立，面包屑路径完整继承)")
        return chunks

chunker = DocumentChunker()
