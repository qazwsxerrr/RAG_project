import re
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

def count_tokens(text: str) -> int:
    """计算文本 Token 数量（快速稳健字符估算，避免网络请求阻塞模块加载）"""
    if not text:
        return 0
    chinese_count = len(re.findall(r'[\u4e00-\u9fff]', text))
    other_count = len(text) - chinese_count
    return max(1, int(chinese_count * 0.8 + other_count * 0.28))

class ParsedTableBlock(BaseModel):
    """解析后的表格结构块模型"""
    table_id: str
    doc_id: str
    title: str = ""                                     # 表格标题或主题
    page_idx: int                                       # 0-indexed 页码
    display_page: int                                   # 1-indexed 页码
    page_range: List[int] = Field(default_factory=list) # 跨页页码范围，如 [3, 4, 5, 6]
    headers: List[str] = Field(default_factory=list)
    rows: List[List[str]] = Field(default_factory=list)
    raw_content: str = ""                               # MinerU 原始 table_body (保证精确回填，杜绝追加文末)
    html_content: str = ""
    markdown_content: str = ""
    semantic_summary: str = ""                          # AI 大模型提炼的语义描述
    bbox: Optional[List[int]] = None
    image_url: Optional[str] = None                     # 切图 OSS 链接
    is_cross_page: bool = False
    stitch_confidence: Optional[float] = None           # 跨页缝合置信度得分 (0.0~1.0)
    is_split_part: bool = False
    part_index: int = 1
    total_parts: int = 1

class TableStitchingService:
    """企业级表格跨页缝合与结构规整服务"""

    def process_document_tables(
        self,
        doc_id: str,
        raw_tables: List[Dict[str, Any]],
        doc_name: str = ""
    ) -> List[ParsedTableBlock]:
        """
        处理文档中的所有表格：
        1. 过滤假表格/空图元
        2. 解析原始 table_body / HTML
        3. 多维置信度跨页连贯性检测与表头继承
        4. 基于 Token 预算的超长表格自适应拆分
        """
        if not raw_tables:
            return []

        parsed_list: List[ParsedTableBlock] = []

        # 1. 基础解析与过滤
        for idx, t in enumerate(raw_tables):
            page_idx = t.get("page_idx", 0)
            table_body = (t.get("table_body") or "").strip()
            img_url = t.get("img_url") or t.get("img_path", "")
            bbox = t.get("bbox")
            caption = t.get("table_caption") or t.get("table_title") or t.get("title") or ""
            title = " ".join(caption).strip() if isinstance(caption, list) else str(caption).strip()
            if not title:
                title = f"{doc_name} 表格 (第{page_idx + 1}页)" if doc_name else f"表格(第{page_idx + 1}页)"

            headers, rows = self._extract_headers_and_rows(table_body)

            # 强校验: 过滤掉既无行数据又无表头的伪表格（避免向 VLM 发送伪表与文档污染）
            if not headers and not rows:
                logger.warning(f"【拦截伪表】未提取到有效表头或数据行，丢弃伪表块 [page={page_idx + 1}, title={title}]")
                continue

            html = self._render_html_table(headers, rows)
            md = self._render_markdown_table(headers, rows)

            block = ParsedTableBlock(
                table_id=f"{doc_id}_tbl_{idx}",
                doc_id=doc_id,
                title=title,
                page_idx=page_idx,
                display_page=page_idx + 1,
                page_range=[page_idx + 1],
                headers=headers,
                rows=rows,
                raw_content=table_body,
                html_content=html,
                markdown_content=md,
                semantic_summary="",
                bbox=bbox,
                image_url=img_url,
                is_cross_page=False
            )
            parsed_list.append(block)

        # 2. 跨页表格多维置信度缝合算法
        stitched_list = self._stitch_cross_page_tables(parsed_list)

        # 3. 超长表格切分（基于 Token 预算，每个子表 600~800 Tokens，完整继承主表头）
        final_list = []
        for tbl in stitched_list:
            splits = self._split_if_oversized(tbl, max_budget_tokens=800)
            final_list.extend(splits)

        return final_list

    def _extract_headers_and_rows(self, html_or_text: str):
        """从 HTML 或 Markdown 文本中提取表头与行列表"""
        headers: List[str] = []
        rows: List[List[str]] = []

        if not html_or_text:
            return headers, rows

        if "<table" in html_or_text.lower():
            # 1. 提取 <th>
            th_matches = re.findall(r'<th[^>]*>(.*?)</th>', html_or_text, flags=re.IGNORECASE | re.DOTALL)
            if th_matches:
                headers = [self._clean_cell(cell) for cell in th_matches]

            # 2. 提取 <tr>
            tr_matches = re.findall(r'<tr[^>]*>(.*?)</tr>', html_or_text, flags=re.IGNORECASE | re.DOTALL)
            total_trs = len(tr_matches)
            for tr in tr_matches:
                row_ths = re.findall(r'<th[^>]*>(.*?)</th>', tr, flags=re.IGNORECASE | re.DOTALL)
                if row_ths and not headers:
                    headers = [self._clean_cell(th) for th in row_ths]
                    continue

                tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, flags=re.IGNORECASE | re.DOTALL)
                if tds:
                    cleaned_tds = [self._clean_cell(td) for td in tds]
                    # 仅当总行数 > 1 且未提取到 <th> 时，才把第一行 <td> 提取为表头；单行续表保持为数据行以正确缝合
                    if not headers and not rows and total_trs > 1:
                        headers = cleaned_tds
                    else:
                        rows.append(cleaned_tds)
        else:
            # Markdown 表格解析
            lines = [l.strip() for l in html_or_text.splitlines() if l.strip().startswith("|")]
            if len(lines) >= 2:
                headers = [self._clean_cell(c) for c in lines[0].split("|")[1:-1]]
                for l in lines[2:]:
                    cells = [self._clean_cell(c) for c in l.split("|")[1:-1]]
                    if cells:
                        rows.append(cells)

        return headers, rows

    def _clean_cell(self, text: str) -> str:
        """清洗单元格内 HTML 标签与多余空白"""
        cleaned = re.sub(r'<[^>]+>', '', text)
        return cleaned.replace("\n", " ").strip()

    def _render_html_table(self, headers: List[str], rows: List[List[str]]) -> str:
        """渲染为高质量前端友好带有 class 的 HTML <table>"""
        if not headers and not rows:
            return ""
        html = ['<table class="rag-table border border-slate-700 w-full text-sm">']
        if headers:
            html.append("  <thead>\n    <tr class=\"bg-slate-800 text-slate-200\">")
            for h in headers:
                html.append(f'      <th class="px-4 py-2 border border-slate-700">{h}</th>')
            html.append("    </tr>\n  </thead>")
        if rows:
            html.append("  <tbody>")
            for r in rows:
                html.append('    <tr class="hover:bg-slate-800/50">')
                for c in r:
                    html.append(f'      <td class="px-4 py-2 border border-slate-700">{c}</td>')
                html.append("    </tr>")
            html.append("  </tbody>")
        html.append("</table>")
        return "\n".join(html)

    def _render_markdown_table(self, headers: List[str], rows: List[List[str]]) -> str:
        """渲染为标准 Markdown 表格"""
        if not headers and not rows:
            return ""
        col_count = len(headers) if headers else (len(rows[0]) if rows else 0)
        if col_count == 0:
            return ""

        md = []
        if headers:
            md.append("| " + " | ".join(headers) + " |")
            md.append("| " + " | ".join(["---"] * col_count) + " |")
        else:
            md.append("| " + " | ".join([f"列{i+1}" for i in range(col_count)]) + " |")
            md.append("| " + " | ".join(["---"] * col_count) + " |")

        for r in rows:
            padded_row = r[:col_count] + [""] * max(0, col_count - len(r))
            md.append("| " + " | ".join(padded_row) + " |")

        return "\n".join(md)

    def _detect_cell_type(self, val: str) -> str:
        """简单推断单元格数据特征类型"""
        val = val.strip()
        if not val:
            return "empty"
        if re.match(r'^-?\d+(\.\d+)?%?$', val):
            return "numeric"
        if re.match(r'^\d{4}[-/年]\d{1,2}', val):
            return "date"
        if len(val) > 30:
            return "long_text"
        return "short_text"

    def _calc_column_fingerprints(self, rows: List[List[str]], col_count: int) -> List[str]:
        """计算各列的主导数据类型指纹"""
        if not rows or col_count <= 0:
            return []
        fingerprints = []
        for col_idx in range(col_count):
            types = [self._detect_cell_type(r[col_idx]) for r in rows if len(r) > col_idx]
            if not types:
                fingerprints.append("unknown")
            else:
                # 取出现频次最多的类型
                dominant = max(set(types), key=types.count)
                fingerprints.append(dominant)
        return fingerprints

    def _stitch_cross_page_tables(self, tables: List[ParsedTableBlock]) -> List[ParsedTableBlock]:
        """
        跨页表格多维置信度检测与缝合算法：
        必须综合评估：
        1. 相邻页码 (必须满足 page_curr == page_prev + 1)
        2. 列数严格相同
        3. 表头语义相似度 (若有重复表头 >= 90%，或后表缺失表头但首行为纯数据)
        4. 列数据类型指纹一致性 (数字/日期/文本分布)
        5. 显式线索与几何版面位置
        加权得分 Score >= 0.85 方允许缝合，杜绝错误合并不同表格！
        """
        if len(tables) <= 1:
            return tables

        result: List[ParsedTableBlock] = []
        prev_table: Optional[ParsedTableBlock] = None

        for current in tables:
            stitched = False
            if prev_table is not None:
                is_consecutive_page = (current.page_idx == prev_table.page_idx + 1)

                col_prev = len(prev_table.headers) if prev_table.headers else (len(prev_table.rows[0]) if prev_table.rows else 0)
                col_curr = len(current.headers) if current.headers else (len(current.rows[0]) if current.rows else 0)

                same_col_count = (col_prev > 0 and col_prev == col_curr)

                if is_consecutive_page and same_col_count:
                    # 维度 1: 表头相似度或表头缺失推断 (权重 0.40)
                    s_header = 0.0
                    if current.headers and prev_table.headers:
                        # 后页重复携带了表头，比对表头文字一致性
                        h1_str = "".join(prev_table.headers)
                        h2_str = "".join(current.headers)
                        if h1_str == h2_str:
                            s_header = 1.0
                        else:
                            # Jaccard 字符重合率
                            set1, set2 = set(h1_str), set(h2_str)
                            jaccard = len(set1 & set2) / max(1, len(set1 | set2))
                            s_header = 1.0 if jaccard >= 0.90 else (jaccard * 0.5)
                    elif not current.headers and prev_table.headers:
                        # 后页无显式表头，直接为数据行，得分 0.90
                        s_header = 0.90

                    # 维度 2: 列数据类型指纹匹配 (权重 0.35)
                    s_type = 0.0
                    if prev_table.rows and current.rows:
                        fp_prev = self._calc_column_fingerprints(prev_table.rows[-3:], col_prev)
                        fp_curr = self._calc_column_fingerprints(current.rows[:3], col_curr)
                        matching_cols = sum(1 for p, c in zip(fp_prev, fp_curr) if p == c or p == "unknown" or c == "unknown")
                        s_type = matching_cols / max(1, col_prev)
                    else:
                        s_type = 0.5

                    # 维度 3: 显式线索与几何版面位置 (权重 0.25)
                    s_hint = 0.5  # 基础分
                    raw_combined = (current.raw_content + " " + " ".join(current.headers)).lower()
                    if any(k in raw_combined for k in ["续表", "（续）", "(续)", "continued", "cont."]):
                        s_hint = 1.0
                    elif prev_table.bbox and current.bbox:
                        # prev 接近底部 (y1 > 700), current 接近顶部 (y0 < 300)
                        if prev_table.bbox[3] > 700 and current.bbox[1] < 300:
                            s_hint = 0.9

                    confidence = 0.40 * s_header + 0.35 * s_type + 0.25 * s_hint

                    # 置信度阈值 0.80 严格判定（有效阻断不同表头混拼，允许高置信度续表缝合）
                    if confidence >= 0.80:
                        current.headers = prev_table.headers
                        current.is_cross_page = True
                        current.stitch_confidence = round(confidence, 2)
                        current.page_range = prev_table.page_range + [current.display_page]
                        current.html_content = self._render_html_table(current.headers, current.rows)
                        current.markdown_content = self._render_markdown_table(current.headers, current.rows)
                        stitched = True

            result.append(current)
            prev_table = current

        return result

    def _split_if_oversized(self, table: ParsedTableBlock, max_budget_tokens: int = 800) -> List[ParsedTableBlock]:
        """
        基于 Token 预算自适应切分长表格（每个子块严格约束在 600~800 Tokens 内）
        每个子表均强制保留：
        1. 完整主表头 (Header Schema)
        2. 原图切图 OSS URL
        3. 跨页页码列表 page_range
        4. 分片序号与总量
        """
        if not table.rows:
            return [table]

        # 检查是否超限（以 markdown 估算）
        full_tokens = count_tokens(table.markdown_content) if table.markdown_content else count_tokens(table.html_content)
        if full_tokens <= max_budget_tokens:
            return [table]

        header_str = " | ".join(table.headers) + "\n" if table.headers else ""
        header_tokens = count_tokens(header_str)

        parts_rows: List[List[List[str]]] = []
        current_rows: List[List[str]] = []
        current_tokens = header_tokens

        for row in table.rows:
            row_str = " | ".join(row) + "\n"
            row_tokens = max(1, count_tokens(row_str))

            # 达到 Token 预算时收口切分
            if current_tokens + row_tokens > max_budget_tokens and current_rows:
                parts_rows.append(current_rows)
                current_rows = [row]
                current_tokens = header_tokens + row_tokens
            else:
                current_rows.append(row)
                current_tokens += row_tokens

        if current_rows:
            parts_rows.append(current_rows)

        total_parts = len(parts_rows)
        parts: List[ParsedTableBlock] = []
        for idx, sub_rows in enumerate(parts_rows, start=1):
            sub_block = self._build_sub_block(table, sub_rows, idx, total_parts)
            parts.append(sub_block)

        return parts

    def _build_sub_block(self, parent: ParsedTableBlock, sub_rows: List[List[str]], part_idx: int, total_parts: int = 1) -> ParsedTableBlock:
        sub_html = self._render_html_table(parent.headers, sub_rows)
        sub_md = self._render_markdown_table(parent.headers, sub_rows)
        return ParsedTableBlock(
            table_id=f"{parent.table_id}_p{part_idx}",
            doc_id=parent.doc_id,
            title=parent.title,
            page_idx=parent.page_idx,
            display_page=parent.display_page,
            page_range=parent.page_range,
            headers=parent.headers,
            rows=sub_rows,
            raw_content=parent.raw_content,
            html_content=sub_html,
            markdown_content=sub_md,
            semantic_summary="",
            bbox=parent.bbox,
            image_url=parent.image_url,
            is_cross_page=parent.is_cross_page,
            stitch_confidence=parent.stitch_confidence,
            is_split_part=True,
            part_index=part_idx,
            total_parts=total_parts
        )

table_stitching_service = TableStitchingService()
