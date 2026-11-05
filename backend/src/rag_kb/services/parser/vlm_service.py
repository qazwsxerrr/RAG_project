import re
import base64
from pathlib import Path
from typing import Dict, Any, Optional, List
import logging
from rag_kb.core.config import settings
from rag_kb.core.llm import llm_gateway

logger = logging.getLogger(__name__)


class MultimodalVLMService:
    """
    多模态视觉理解与结构化语义提炼服务
    负责文档配图、流程图架构、表格切图与代码块的高精度语义解析。
    全程委托全局弹性网关 llm_gateway.chat_sync 执行，自动共享视觉模态候选池筛选、短期租约换票与毫秒级热切换 (Phase-7)。
    """

    @property
    def base_url(self) -> str:
        return settings.active_llm_base_url

    @property
    def model(self) -> str:
        return settings.active_llm_model

    def _call_gateway(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 800,
        temperature: float = 0.1,
        action_name: str = "多模态视觉 API",
        backend: Optional[str] = None
    ) -> str:
        """统一通过全局弹性大模型网关执行同步推理，遇到主节点故障自动切换至可用候选节点"""
        try:
            content = llm_gateway.chat_sync(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                backend=backend
            )
            if not content or not str(content).strip():
                raise ValueError(f"【{action_name}错误】: 模型返回内容为空！")
            return str(content).strip()
        except Exception as e:
            logger.error(f"【{action_name}执行异常】: {e}")
            raise

    @staticmethod
    def _strip_heading_hashes(text: str) -> str:
        cleaned_lines = [re.sub(r'^(?:#{1,6}\s+)', '', line) for line in text.strip().splitlines()]
        return "\n".join(cleaned_lines).strip()

    def describe_image(self, image_url_or_path: str, context: Dict[str, Any]) -> str:
        """结合所属文档名、标题章节与上下文，调用多模态视觉节点生成图文摘要"""
        doc_name = context.get("doc_name", "技术文档")
        heading = context.get("heading", "正文小节")
        surrounding_text = context.get("surrounding_text", "")

        prompt = f"""请结合提供的图片及上下文，用一段连贯、客观的文字对图片进行核心语义解析。
【所属文档】：{doc_name}
【所属章节】：{heading}
【上下文信息】：{surrounding_text[:300]}

【要求】：
1. 以单段纯文本直接陈述，概括核心主题。
2. 提炼图中的关键架构层级、具体实体/产品名称及流转逻辑。
3. 语言紧凑精炼，关键技术术语保留原样。
"""
        formatted_img_url = self._format_image_payload(image_url_or_path)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": formatted_img_url}},
                ],
            }
        ]
        content = self._call_gateway(messages, max_tokens=800, temperature=0.1, action_name="多模态视觉 API")
        return self._strip_heading_hashes(content)

    def describe_flowchart(self, image_url_or_path: str, context: Dict[str, Any]) -> str:
        """针对系统架构图、业务流程图、时序图等图像，提取架构分层、核心组件与流转拓扑"""
        doc_name = context.get("doc_name", "技术文档")
        heading = context.get("heading", "正文小节")
        surrounding_text = context.get("surrounding_text", "")

        prompt = f"""请结合提供的系统架构/业务流程图片及上下文，进行高信息密度的拓扑语义提炼：

【所属文档】：{doc_name}
【所属章节】：{heading}
【上下文信息】：{surrounding_text[:300]}

【提炼准则】：
- 直接阐明图表的核心业务主题。
- 简单图表用 1-2 句话概括其流转走向；复杂图表按层级说明关键模块，并按箭头流向陈述调用链路。
- 纯文本直接陈述客观事实，语言紧凑精炼。
"""
        formatted_img_url = self._format_image_payload(image_url_or_path)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": formatted_img_url}},
                ],
            }
        ]
        content = self._call_gateway(messages, max_tokens=1200, temperature=0.1, action_name="多模态流程图 API")
        return self._strip_heading_hashes(content)

    def describe_table(self, table_content: str, context: Dict[str, Any], table_image_url: Optional[str] = None) -> str:
        """调用大模型对表格进行深度结构化语义解析与纯文本序列化"""
        doc_name = context.get("doc_name", "技术文档")
        heading = context.get("heading", "正文小节")
        table_title = context.get("title", "") or heading

        if not (table_content and table_content.strip()) and not table_image_url:
            raise RuntimeError("【表格描述错误】表格内容与表格图片链接均为空，无法进行语义提取！")

        prompt = f"""你是一名严谨的企业级数据结构化与知识工程专家。请将以下表格严格转化为标准语义纯文本格式。
【所属文档】：{doc_name}
【所属章节/表名】：{table_title}
【输入表格数据】：
{table_content}

【强制输出语法规范（必须严格遵循，违者直接视为错误）】：
1. 【首行元数据声明】：
   必须且仅能以如下句式开头：
   "该表给出 [表格主题]，共 [N] 列：[列名1]、[列名2]、[列名3]...，按[第一列维度名]组织[表格主题]详情。"
   其中 N 为准确列数，列名必须严格按照原表顺序以顿号“、”连接。

2. 【数据行严格映射（每个数据行之间空一行隔开）】：
   每一行必须严格按照：
   [第一列内容]：[第二列内容]；[第三列内容]；...
   - 必须以第一列作为主键，后接全角冒号“：”；
   - 第二列及后续所有列的内容，必须严格按列顺序以全角分号“；”作为分隔符连接；
   - 单元格内部的并列逗号统一使用半角逗号“,”，严禁在列内擅自使用分号；
   - 必须 100% 忠实于原表文字，严禁擅自删减、概括、润色或脑补任何事实内容，保持一字不差！

3. 【排版与防幻觉禁令】：
   - 严禁输出任何 Markdown 管道表格符号（如 |---|---| 等）；
   - 严禁输出任何 HTML 标签（如 <table>、<tr>、<td> 等）；
   - 严禁输出任何多余的开场白、说明文字、结束语或客套话；
   - 必须严格忠实于输入的【输入表格数据】，若输入数据无法成表，请仅如实描述原句，严禁抄袭或套用下方的参考示例！

【标准示例参考】：
该表给出 RAG 与 Fine-tuning (微调) 的对比，共 3 列：维度、RAG、Fine-tuning (微调)，按维度组织对比两种方法在各维度上的差异。

是否改变模型权重：否,只改 Prompt 输入；是,更新模型参数

知识来源：外部向量库,可动态增删；固化在权重里

知识更新成本：低,重新索引文档即可；高,需要重新训练/部署

是否有溯源：可引用来源文档；无
"""
        content_payload: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        if table_image_url:
            from rag_kb.core.config import WORKSPACE_ROOT as BASE_DIR
            is_remote = table_image_url.startswith("http://") or table_image_url.startswith("https://")
            is_local = Path(table_image_url).exists() or (BASE_DIR / table_image_url).exists()
            if is_remote or is_local:
                formatted_url = self._format_image_payload(table_image_url)
                content_payload.append({"type": "image_url", "image_url": {"url": formatted_url}})

        messages = [{"role": "user", "content": content_payload}]
        return self._call_gateway(messages, max_tokens=800, temperature=0.1, action_name="表格描述 API")

    def describe_code(self, code_content: str, language: str, context: Dict[str, Any]) -> str:
        """调用大模型对代码块进行深度语义理解与业务逻辑提取"""
        doc_name = context.get("doc_name", "技术文档")
        heading = context.get("heading", "正文小节")

        prompt = f"""请对以下代码进行高信息密度的语义提炼，直接陈述其核心业务功能与关键操作：

【所属文档】：{doc_name}
【所属章节】：{heading}
【代码语言】：{language}
【输入代码】：
```{language}
{code_content}
```

【提炼准则】：
- 局部片段或单行语句（如 SQL/过滤条件/配置项）：用 1-2 句话直接说明操作对象（表名、字段、配置键）与业务目的。
- 完整函数或复杂模块：直接陈述核心函数/类名、关键输入输出与核心执行逻辑。
- 纯文本直接输出技术事实，保持语言紧凑精炼。
"""
        messages = [{"role": "user", "content": prompt}]
        content = self._call_gateway(messages, max_tokens=1500, temperature=0.1, action_name="代码块语义解析 API")
        return self._strip_heading_hashes(content)

    def describe_flowchart_text(self, mermaid_code: str, context: Dict[str, Any]) -> str:
        """调用大模型对 Mermaid 文本流程图/架构图/时序图进行系统化语义解析"""
        doc_name = context.get("doc_name", "技术文档")
        heading = context.get("heading", "正文小节")
        title = context.get("title", "系统流程图")

        prompt = f"""请对以下 Mermaid 流程图/架构图提取核心拓扑流转语义：

【所属文档】：{doc_name}
【所属章节】：{heading}
【流程图主题】：{title}
【Mermaid 代码】：
```mermaid
{mermaid_code}
```

【提炼准则】：
- 简单图表（1~3 个节点）：用 1 句话直接写出主题与流向（如 `[起点] -> [终点]`）。
- 复杂分层或多分支图表：按层级列出关键组件，并按调用顺序陈述主干流转链路（如 `模块A --(动作)--> 模块B`）。
- 纯文本直接陈述流转事实，保持高信息密度与语言紧凑。
"""
        messages = [{"role": "user", "content": prompt}]
        content = self._call_gateway(messages, max_tokens=1500, temperature=0.1, action_name="流程图语义解析 API")
        return self._strip_heading_hashes(content)

    def probe_model_connection(self) -> Dict[str, Any]:
        """测试并验证网关候选池连通性与响应"""
        messages = [{"role": "user", "content": "请回复'API Connection OK'"}]
        reply = self._call_gateway(messages, max_tokens=30, temperature=0.1, action_name="模型连通性探测")
        return {
            "status": "success",
            "model": self.model,
            "base_url": self.base_url,
            "response": reply.strip()
        }

    def _format_image_payload(self, image_url_or_path: str) -> str:
        """处理图片链接：支持云端 OSS HTTP(S) URL（私有桶自动临时授权签名）或本地 Base64 编码注入"""
        if image_url_or_path.startswith("http://") or image_url_or_path.startswith("https://"):
            if "aliyuncs.com" in image_url_or_path and "OSSAccessKeyId" not in image_url_or_path:
                from rag_kb.utils.oss import oss_service
                return oss_service.sign_url(image_url_or_path, expires=3600)
            return image_url_or_path

        if image_url_or_path.startswith("data:image"):
            return image_url_or_path

        from rag_kb.core.config import WORKSPACE_ROOT as BASE_DIR
        local_path = Path(image_url_or_path)
        if not local_path.exists():
            candidate = BASE_DIR / image_url_or_path
            if candidate.exists():
                local_path = candidate

        if local_path.exists() and local_path.is_file():
            with open(local_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")
            suffix = local_path.suffix.lower().replace(".", "")
            mime = "jpeg" if suffix in ["jpg", "jpeg"] else suffix
            return f"data:image/{mime};base64,{encoded}"

        return image_url_or_path


vlm_service = MultimodalVLMService()
