import re
import uuid
import logging
from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

class ParsedCodeBlock(BaseModel):
    """解析后的代码块模型"""
    block_id: str
    doc_id: str
    language: str = "text"
    raw_code: str
    semantic_summary: str = ""
    page_idx: int = 0
    display_page: int = 1
    line_number: Optional[int] = None
    title: str = ""

class ParsedFlowchartBlock(BaseModel):
    """解析后的流程图/架构图模型"""
    chart_id: str
    doc_id: str
    chart_type: str = "mermaid"  # mermaid | image
    raw_content: str = ""
    semantic_summary: str = ""
    image_url: Optional[str] = None
    page_idx: int = 0
    display_page: int = 1
    line_number: Optional[int] = None
    title: str = ""


class CodeFlowchartService:
    """企业级代码块与流程图解析增强服务"""

    FLOWCHART_KEYWORDS = {
        "流程", "架构", "时序", "时序图", "拓扑", "体系结构", "流转", "系统图", "交互图",
        "数据流", "调用链路", "flowchart", "architecture", "diagram", "pipeline", "workflow"
    }

    def is_flowchart_image(self, img_dict: Dict[str, Any]) -> bool:
        """根据标题、alt、文件名判断图片是否属于系统架构图或流程图"""
        alt = str(img_dict.get("alt", "")).lower()
        heading = str(img_dict.get("heading", "")).lower()
        img_path = str(img_dict.get("img_path", "")).lower()

        combined = f"{alt} {heading} {img_path}"
        return any(kw in combined for kw in self.FLOWCHART_KEYWORDS)

    def extract_blocks_from_markdown(
        self,
        doc_id: str,
        markdown_text: str,
        content_list: Optional[List[Dict[str, Any]]] = None,
        doc_name: str = ""
    ) -> Tuple[List[ParsedCodeBlock], List[ParsedFlowchartBlock]]:
        """
        从 Markdown 文本中提取所有代码块与 Mermaid 流程图，并计算物理页码与行号。
        代码与流程图的语义总结均由 Step 6 真实调用 AI 模型完成，绝不使用本地脚本或 AST 规则合成。
        """
        code_blocks: List[ParsedCodeBlock] = []
        flowcharts: List[ParsedFlowchartBlock] = []

        if not markdown_text:
            return code_blocks, flowcharts

        md_lines = markdown_text.splitlines()

        in_fence = False
        fence_lang = ""
        fence_start_line = 1
        fence_lines: List[str] = []

        for l_idx, line in enumerate(md_lines, start=1):
            if not in_fence:
                # 检查代码围栏开启: 允许任意缩进，以 ``` 开头
                fence_match = re.match(r'^[ \t]*```([a-zA-Z0-9_-]+)?\s*$', line)
                if fence_match:
                    in_fence = True
                    fence_lang = (fence_match.group(1) or "").strip().lower()
                    fence_start_line = l_idx
                    fence_lines = [line]
            else:
                fence_lines.append(line)
                # 检查代码围栏闭合: 允许任意缩进，匹配 ``` 独立闭合行
                if re.match(r'^[ \t]*```\s*$', line):
                    in_fence = False
                    raw_block = "\n".join(fence_lines)
                    code_content = "\n".join(fence_lines[1:-1]).strip()
                    title = self._find_nearest_heading(md_lines, fence_start_line) or doc_name
                    page_idx = self._resolve_page_idx(code_content, content_list, fence_start_line, len(md_lines))
                    display_page = page_idx + 1

                    if fence_lang == "mermaid" or self._is_mermaid_syntax(code_content):
                        chart_id = f"flow_{uuid.uuid4().hex[:8]}"
                        flowcharts.append(ParsedFlowchartBlock(
                            chart_id=chart_id,
                            doc_id=doc_id,
                            chart_type="mermaid",
                            raw_content=raw_block,
                            semantic_summary="",  # 真实调用 AI 模型完成
                            page_idx=page_idx,
                            display_page=display_page,
                            line_number=fence_start_line,
                            title=f"流程图 (Mermaid) (第 {display_page} 页)"
                        ))
                    else:
                        block_id = f"code_{uuid.uuid4().hex[:8]}"
                        code_blocks.append(ParsedCodeBlock(
                            block_id=block_id,
                            doc_id=doc_id,
                            language=fence_lang or "code",
                            raw_code=raw_block,
                            semantic_summary="",  # 真实调用 AI 模型完成
                            page_idx=page_idx,
                            display_page=display_page,
                            line_number=fence_start_line,
                            title=f"代码块 ({fence_lang or 'code'}) (第 {display_page} 页)"
                        ))
                    fence_lines = []

        # 容错处理: 若 Markdown 文档结尾处代码围栏未闭合，安全收口
        if in_fence and fence_lines:
            raw_block = "\n".join(fence_lines)
            code_content = "\n".join(fence_lines[1:]).strip()
            title = self._find_nearest_heading(md_lines, fence_start_line) or doc_name
            page_idx = self._resolve_page_idx(code_content, content_list, fence_start_line, len(md_lines))
            display_page = page_idx + 1

            if fence_lang == "mermaid" or self._is_mermaid_syntax(code_content):
                chart_id = f"flow_{uuid.uuid4().hex[:8]}"
                flowcharts.append(ParsedFlowchartBlock(
                    chart_id=chart_id,
                    doc_id=doc_id,
                    chart_type="mermaid",
                    raw_content=raw_block,
                    semantic_summary="",
                    page_idx=page_idx,
                    display_page=display_page,
                    line_number=fence_start_line,
                    title=f"流程图 (Mermaid) (第 {display_page} 页)"
                ))
            else:
                block_id = f"code_{uuid.uuid4().hex[:8]}"
                code_blocks.append(ParsedCodeBlock(
                    block_id=block_id,
                    doc_id=doc_id,
                    language=fence_lang or "code",
                    raw_code=raw_block,
                    semantic_summary="",
                    page_idx=page_idx,
                    display_page=display_page,
                    line_number=fence_start_line,
                    title=f"代码块 ({fence_lang or 'code'}) (第 {display_page} 页)"
                ))

        return code_blocks, flowcharts

    def _is_mermaid_syntax(self, code: str) -> bool:
        """检查代码内容是否具备 Mermaid 语法特征"""
        first_line = code.strip().splitlines()[0].strip() if code.strip().splitlines() else ""
        return any(first_line.startswith(prefix) for prefix in [
            "graph", "flowchart", "sequenceDiagram", "stateDiagram", "classDiagram", "erDiagram", "gantt", "pie"
        ])

    def _find_nearest_heading(self, md_lines: List[str], target_line: int) -> str:
        """向上追溯最近的一级/二级/三级标题"""
        for i in range(min(target_line - 1, len(md_lines) - 1), -1, -1):
            line = md_lines[i].strip()
            if line.startswith("#"):
                return re.sub(r'^#{1,6}\s*', '', line).strip()
        return ""

    def _resolve_page_idx(
        self,
        snippet: str,
        content_list: Optional[List[Dict[str, Any]]],
        line_no: int,
        total_lines: int
    ) -> int:
        """匹配物理页码"""
        if content_list:
            clean_sub = snippet[:40].strip()
            if clean_sub:
                for item in content_list:
                    it_text = str(item.get("text", "")).strip()
                    if clean_sub in it_text or it_text in clean_sub:
                        return item.get("page_idx", 0)

            # 根据 content_list 中最大页码估算行比例
            max_page = max((item.get("page_idx", 0) for item in content_list), default=0)
            if max_page > 0 and total_lines > 0:
                est_page = int((line_no / total_lines) * max_page)
                return min(max_page, max(0, est_page))

        return 0


# 全局单例
code_flowchart_service = CodeFlowchartService()
