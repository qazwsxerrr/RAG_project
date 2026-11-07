"""src/rag_kb/services/pipeline/cleaning_service.py —— 海量异构文档双轨分级数据清洗与准入质检服务。

严格对齐 new-command.md (§5 数据处理与清洗规范) 与 对话.txt (第 43~47 行)：
1. 非专业通用文档（如：技术规范、日常通知、培训手册、操作指南、FAQ、行业报告等）：
   - 执行全自动格式化清洗：剥离控制字符/零宽乱码、剔除跨页重复页眉页脚噪点、规整 Markdown 标题与代码块围栏、压缩冗余空行，随后直接平滑入库。
2. 专业严谨性文档（如：财务报表、法务合同、审计报告、招投标书、合规制度、医疗/金融报表等）：
   - 执行严格结构准入质检（Strict Schema Gate），严禁 AI 越权代改或脑补残缺数据！
   - 质检规则：
     (a) 有效正文与版面可读性校验（拦截空白扫描件、严重 OCR 乱码污染）；
     (b) 表格结构完整性强检（拦截缺失表头、空壳残表、数据行列数与表头严重错位缺列）。
   - 一旦检出格式不达标，立即抛出 DocumentFormatInvalidError ([FORMAT_INVALID])，终止入库并明确引导业务人员重新整理规范后再提交。
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)

# 专业严谨性文档分类关键词集合（命中任一关键词即进入“只检不改、残缺必拦”严谨准入轨道）
STRICT_PROFESSIONAL_KEYWORDS = (
    "财务",
    "财报",
    "会计",
    "审计",
    "决算",
    "预算表",
    "合同",
    "法务",
    "协议",
    "招投标",
    "标书",
    "合规",
    "医疗",
    "病历",
    "处方",
    "金融报表",
    "资产负债",
)

# 不可见非法控制字符（保留 \t \n \r）
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 零宽与 BOM 等隐形干扰符
_ZERO_WIDTH_RE = re.compile(r"[\ufeff\u200b\u200c\u200d\u2060]")
# 独立纯页码噪点行（如 "- 12 -", "Page 3 of 20", "第 5 页 / 共 18 页"）
_PAGE_FOOTER_NOISE_RE = re.compile(
    r"^\s*(?:[-—–_]+\s*\d{1,4}\s*[-—–_]+|[-—–_]\s*\d{1,4}\s*[-—–_]|page\s+\d+\s*(?:of\s+\d+)?|第\s*\d+\s*页(?:\s*[/／]\s*共?\s*\d+\s*页|\s*共\s*\d+\s*页)?)\s*$",
    re.IGNORECASE,
)
# Markdown 标题缺少空格规整（如 "###第一章" -> "### 第一章"，排除 "###" 纯井号分割线）
_MALFORMED_HEADING_RE = re.compile(r"^(#{1,6})([^\s#].*)$")


class DocumentFormatInvalidError(ValueError):
    """专业严谨文档格式残缺/错乱准入拦截异常（前端引导用户重新整理后再提交）"""

    def __init__(self, message: str, error_code: str = "FORMAT_INVALID"):
        self.error_code = error_code
        full_msg = f"[{error_code}] {message}" if not message.startswith(f"[{error_code}]") else message
        super().__init__(full_msg)


@dataclass
class CleaningReport:
    """数据清洗与质检执行报告"""
    is_strict_professional: bool
    cleaned_markdown: str
    removed_control_chars: int = 0
    removed_noise_lines: int = 0
    normalized_headings: int = 0
    checked_tables: int = 0
    actions: List[str] = field(default_factory=list)


class DataCleaningService:
    """企业级海量异构文档分级清洗与质检引擎"""

    GARBLED_RATIO_THRESHOLD = 0.05  # 乱码替换符占比上限 5%
    MIN_PROFESSIONAL_TEXT_LEN = 20  # 专业文档最少有效字符数

    def is_professional_document(self, category: str, tags: List[str] = None, file_name: str = "") -> bool:
        """判定是否属于严禁越权代改的专业严谨文档（将分类、标签以及文件名全面纳入关键词判定）"""
        probe_parts = [category or ""]
        if tags:
            probe_parts.extend(str(t) for t in tags if t)
        if file_name:
            probe_parts.append(str(file_name))
        probe_text = " ".join(probe_parts).lower()
        return any(kw.lower() in probe_text for kw in STRICT_PROFESSIONAL_KEYWORDS)

    def inspect_and_clean(
        self,
        markdown_text: str,
        content_list: List[Dict[str, Any]],
        category: str = "技术规范",
        tags: List[str] = None,
        file_name: str = "",
    ) -> CleaningReport:
        """
        执行双轨分级清洗或准入质检：
        - 专业严谨文档：先做严格结构与乱码预检，不达标立即抛 DocumentFormatInvalidError；达标后仅做无损空白符清理，绝不篡改正文与表格数据；
        - 通用非专业文档：执行全自动去噪与版式标准化清洗。
        """
        raw_md = markdown_text or ""
        elements = content_list or []
        is_strict = self.is_professional_document(category=category, tags=tags or [], file_name=file_name)

        if is_strict:
            self._validate_strict_professional_document(
                raw_md=raw_md,
                elements=elements,
                category=category,
                file_name=file_name,
            )
            # 专业文档通过质检后：仅剥离底层不可见非法控制符，保持原始业务措辞与表格结构 100% 原貌
            clean_md, ctrl_cnt = self._strip_invisible_garbage(raw_md)
            clean_md = re.sub(r"\n{4,}", "\n\n\n", clean_md).strip()
            return CleaningReport(
                is_strict_professional=True,
                cleaned_markdown=clean_md,
                removed_control_chars=ctrl_cnt,
                checked_tables=sum(1 for e in elements if e.get("type") == "table"),
                actions=["strict_schema_verified", "lossless_control_char_strip"],
            )

        # 通用非专业文档：执行全自动标准化格式清洗
        return self._auto_clean_general_document(raw_md=raw_md, elements=elements)

    # ==================== 轨道 A：通用非专业文档全自动清洗 ====================

    def _auto_clean_general_document(
        self,
        raw_md: str,
        elements: List[Dict[str, Any]],
    ) -> CleaningReport:
        clean_md, ctrl_cnt = self._strip_invisible_garbage(raw_md)
        clean_md = clean_md.replace("\r\n", "\n").replace("\r", "\n")

        lines = clean_md.split("\n")

        # 第一遍统计：跳过代码块内部，统计正文中跨页高频重复出现 >= 4 次的页眉/水印噪点
        freq_map: Dict[str, int] = {}
        in_fence_pass1 = False
        for ln in lines:
            s = ln.strip()
            if s.startswith("```"):
                in_fence_pass1 = not in_fence_pass1
                continue
            if not in_fence_pass1:
                if 2 <= len(s) <= 45 and not s.startswith(("#", "|", "![", "<table", "<tr", "<td", "-", "*")):
                    freq_map[s] = freq_map.get(s, 0) + 1
        repeated_headers: Set[str] = {txt for txt, cnt in freq_map.items() if cnt >= 4}

        out_lines: List[str] = []
        removed_noise = 0
        norm_headings = 0
        in_code_fence = False

        for ln in lines:
            stripped = ln.strip()
            # 优先识别代码块围栏切换
            if stripped.startswith("```"):
                in_code_fence = not in_code_fence
                out_lines.append(ln.rstrip())
                continue

            # 在代码块围栏内部的代码行，必须原样无损保留，严禁误删或篡改
            if in_code_fence:
                out_lines.append(ln.rstrip())
                continue

            # 1. 过滤正文中跨页高频重复出现的页眉/水印行或页码噪点
            if stripped in repeated_headers or _PAGE_FOOTER_NOISE_RE.match(stripped):
                removed_noise += 1
                continue

            # 2. 剔除乱码替换符 '\ufffd'
            if "\ufffd" in ln:
                ln = ln.replace("\ufffd", "")

            # 3. 规整紧贴井号的 Markdown 标题 (如 "##系统架构" -> "## 系统架构")
            m_head = _MALFORMED_HEADING_RE.match(ln.strip())
            if m_head:
                ln = f"{m_head.group(1)} {m_head.group(2).strip()}"
                norm_headings += 1

            out_lines.append(ln.rstrip())

        # 4. 若存在未闭合的代码块围栏，自动补齐闭合标记防止切分器错乱
        actions = ["general_auto_format"]
        if in_code_fence:
            out_lines.append("```")
            actions.append("auto_closed_code_fence")

        merged = "\n".join(out_lines)
        # 5. 压缩连续超过 2 行的空白行为标准双换行段落
        merged = re.sub(r"\n{3,}", "\n\n", merged).strip()

        if not merged:
            raise DocumentFormatInvalidError(
                f"文档清洗后无任何有效可读文本内容，请检查源文件是否为空白页或纯图片无字扫描件。"
            )

        return CleaningReport(
            is_strict_professional=False,
            cleaned_markdown=merged,
            removed_control_chars=ctrl_cnt,
            removed_noise_lines=removed_noise,
            normalized_headings=norm_headings,
            checked_tables=sum(1 for e in elements if e.get("type") == "table"),
            actions=actions,
        )

    # ==================== 轨道 B：专业严谨文档严苛预检（拒绝越权代改） ====================

    def _validate_strict_professional_document(
        self,
        raw_md: str,
        elements: List[Dict[str, Any]],
        category: str,
        file_name: str,
    ) -> None:
        stripped_text = re.sub(r"\s+", "", raw_md or "")
        tables_in_elements = [el for el in elements if el.get("type") == "table"]

        # 1. 空白或扫描缺失检测
        if len(stripped_text) < self.MIN_PROFESSIONAL_TEXT_LEN and not tables_in_elements:
            raise DocumentFormatInvalidError(
                f"专业类文档（分类：{category}）有效文本不足 {self.MIN_PROFESSIONAL_TEXT_LEN} 字且无有效结构化表格。"
                f"为避免解析失真，系统拒绝入库，请重新整理提供清晰可解析的规范文档后再提交。"
            )

        # 2. 严重乱码率拦截（如字体编码损坏导致的  或控制乱码）
        if stripped_text:
            garbled_cnt = raw_md.count("\ufffd") + len(_CONTROL_CHARS_RE.findall(raw_md))
            garbled_ratio = garbled_cnt / max(1, len(stripped_text))
            if garbled_ratio > self.GARBLED_RATIO_THRESHOLD:
                raise DocumentFormatInvalidError(
                    f"专业类文档（分类：{category}）检出严重字符编码乱码（乱码率 {garbled_ratio:.1%} > 阈值 5%）。"
                    f"严禁系统自动猜测篡改专业文本，请修复源文件字体编码或重新导出规范版本后再提交。"
                )

        # 3. 校验 content_list 中的 HTML / Markdown 表格结构完整性
        for idx, tbl in enumerate(tables_in_elements):
            page_no = int(tbl.get("page_idx", 0)) + 1
            body = (tbl.get("table_body") or "").strip()
            self._assert_professional_table_valid(body, page_no=page_no, table_seq=idx + 1, category=category)

        # 4. 校验原生 Markdown 内嵌管道表格（针对直接上传 .md 专业合同/报表场景）
        self._validate_markdown_pipe_tables(raw_md, category=category)

    def _assert_professional_table_valid(
        self,
        table_body: str,
        page_no: int,
        table_seq: int,
        category: str,
    ) -> None:
        if not table_body:
            raise DocumentFormatInvalidError(
                f"专业类文档（{category}）第 {page_no} 页第 {table_seq} 个表格内容完全残缺为空。"
                f"系统拒绝由 AI 猜测补全，请重新整理表格结构后再提交。"
            )

        lower_body = table_body.lower()
        if "<table" in lower_body or "<tr" in lower_body:
            tr_blocks = re.findall(r"<tr[^>]*>(.*?)</tr>", table_body, flags=re.IGNORECASE | re.DOTALL)
            if len(tr_blocks) < 2:
                raise DocumentFormatInvalidError(
                    f"专业类文档（{category}）第 {page_no} 页第 {table_seq} 个表格仅检出 {len(tr_blocks)} 行，"
                    f"缺少有效表头或数据行，请重新整理规范表格后再提交。"
                )

            row_col_counts: List[int] = []
            header_cells: List[str] = []
            has_colspan_or_rowspan = bool(re.search(r"\b(?:colspan|rowspan)\s*=", lower_body))

            for r_idx, tr in enumerate(tr_blocks):
                cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.IGNORECASE | re.DOTALL)
                cleaned_cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
                if r_idx == 0:
                    header_cells = cleaned_cells
                if cleaned_cells:
                    row_col_counts.append(len(cleaned_cells))

            # 表头全空校验
            if not header_cells or all(not c for c in header_cells):
                raise DocumentFormatInvalidError(
                    f"专业类文档（{category}）第 {page_no} 页第 {table_seq} 个表格表头全部为空（缺头残表）。"
                    f"系统严禁越权代改表头字段，请补全表头后再提交。"
                )

            # 非合并单元格情况下，校验数据行列数是否大面积错位缺列
            if not has_colspan_or_rowspan and len(header_cells) >= 2 and len(row_col_counts) >= 2:
                expected_cols = len(header_cells)
                mismatched_rows = sum(1 for c_cnt in row_col_counts[1:] if c_cnt != expected_cols)
                if mismatched_rows / max(1, len(row_col_counts) - 1) > 0.5:
                    raise DocumentFormatInvalidError(
                        f"专业类文档（{category}）第 {page_no} 页第 {table_seq} 个表格检出严重缺列/错位"
                        f"（表头 {expected_cols} 列，超过半数数据行列数不匹配）。请重新整理规范排版后再提交。"
                    )

    def _validate_markdown_pipe_tables(self, raw_md: str, category: str) -> None:
        """校验 Markdown 原生管道表格是否存在空表头或严重缺列错位"""
        lines = raw_md.splitlines()
        i = 0
        tbl_seq = 0
        while i < len(lines) - 1:
            line = lines[i].strip()
            next_line = lines[i + 1].strip()
            # 识别 Markdown 表头 + 分隔线 (如 | A | B | \n | --- | --- |)
            if (
                line.startswith("|")
                and line.endswith("|")
                and next_line.startswith("|")
                and re.match(r"^\|[\s:\-\|]+\|$", next_line)
                and "-" in next_line
            ):
                tbl_seq += 1
                header_cols = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
                sep_cols = [c.strip() for c in re.split(r"(?<!\\)\|", next_line.strip("|"))]

                if not header_cols or all(not c for c in header_cols) or any(not c for c in header_cols):
                    raise DocumentFormatInvalidError(
                        f"专业类文档（{category}）第 {tbl_seq} 个表格存在缺失表头列名（空表头单元）。"
                        f"系统拒绝自动脑补列名，请补全表头并重新整理后再提交。"
                    )

                if len(header_cols) != len(sep_cols):
                    raise DocumentFormatInvalidError(
                        f"专业类文档（{category}）第 {tbl_seq} 个表格表头（{len(header_cols)}列）与分隔定义（{len(sep_cols)}列）错位。"
                        f"请重新整理规范后再提交。"
                    )

                expected_cols = len(header_cols)
                data_rows = []
                j = i + 2
                while j < len(lines):
                    r_line = lines[j].strip()
                    if not (r_line.startswith("|") and r_line.endswith("|")):
                        break
                    r_cols = [c.strip() for c in re.split(r"(?<!\\)\|", r_line.strip("|"))]
                    data_rows.append(r_cols)
                    j += 1

                if not data_rows:
                    raise DocumentFormatInvalidError(
                        f"专业类文档（{category}）第 {tbl_seq} 个表格仅有表头无任何数据行，请核对整理后再提交。"
                    )

                mismatch_cnt = sum(1 for r in data_rows if len(r) != expected_cols)
                if mismatch_cnt > 0:
                    raise DocumentFormatInvalidError(
                        f"专业类文档（{category}）第 {tbl_seq} 个表格检出列数残缺错位"
                        f"（表头 {expected_cols} 列，其中 {mismatch_cnt} 行数据列数不符）。"
                        f"为杜绝专业数据失真，请重新整理表格后再提交。"
                    )
                i = j
                continue
            i += 1

    @staticmethod
    def _strip_invisible_garbage(text: str):
        cleaned, c1 = _CONTROL_CHARS_RE.subn("", text)
        cleaned, c2 = _ZERO_WIDTH_RE.subn("", cleaned)
        return cleaned, (c1 + c2)


cleaning_service = DataCleaningService()
