import os
import json
import re
import shutil
import time
import zipfile
import requests
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from backend.app.core.config import settings
from backend.app.core.oss import oss_service

logger = logging.getLogger(__name__)

class MinerUParserAdapter:
    """
    MinerU 版面分析引擎适配器
    负责解析各类技术文档 (PDF, DOCX, PPTX, MD)，
    并在 data/parsed/{doc_id}/ 规整产出:
      - full.md (排版结构)
      - layout.json (版面树)
      - {doc_id}_content_list.json (图元列表，含 bbox 与 page_idx)
      - images/ (切图目录)
    """

    def __init__(self, base_parsed_dir: Optional[str] = None):
        self.base_parsed_dir = Path(base_parsed_dir or settings.DATA_PARSED_DIR)
        self.base_parsed_dir.mkdir(parents=True, exist_ok=True)

    MINERU_SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}

    def parse_document(
        self,
        doc_id: str,
        file_path: str,
        file_name: str,
        file_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """执行文档深度解析与结构化产出"""
        doc_dir = self.base_parsed_dir / doc_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        images_dir = doc_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        ext = Path(file_name).suffix.lower()

        # 复杂版式与富媒体文档（PDF, DOCX, DOC, PPTX, PPT, XLS, XLSX）
        # 统一采用 MinerU 官方云端 API 进行高精版面分析、公式识别、表格提取与高清切图
        if ext in self.MINERU_SUPPORTED_EXTENSIONS:
            parser_mode = getattr(settings, "MINERU_PARSER_MODE", "auto").lower()
            if parser_mode == "agent":
                logger.info(f"[MinerU] 解析模式配置为 agent 极速模式，直接调用 Agent 解析接口 (doc_id={doc_id})")
                return self._parse_document_via_agent_api(doc_id, file_path, file_name, file_url, doc_dir, images_dir)
            return self._parse_document_via_mineru_api(doc_id, file_path, file_name, file_url, doc_dir, images_dir)

        # Markdown / 文本类原生轻量文档解析（纯文本结构，绝不伪造像素坐标与虚假图片路径）
        if ext in [".md", ".markdown", ".txt", ".json"]:
            return self._parse_text_markdown_document(doc_id, file_path, file_name, doc_dir, images_dir)

        raise ValueError(
            f"【不支持的文件格式】: {ext}。MinerU 官方 API 支持: {', '.join(sorted(self.MINERU_SUPPORTED_EXTENSIONS))}，原生支持: .md, .txt"
        )

    def _parse_document_via_mineru_api(
        self,
        doc_id: str,
        file_path: str,
        file_name: str,
        file_url: Optional[str],
        doc_dir: Path,
        images_dir: Path
    ) -> Dict[str, Any]:
        """
        调用官方 MinerU 云端 API (https://mineru.net/api/v4/extract/task)
        进行高精版面解析（包含 OCR、表格提取、公式解析等）。
        严格遵循 Fail-Fast 原则，绝无本地伪造与 Mock 降级。
        """
        api_key = settings.MINERU_API_KEY.strip()
        if not api_key:
            raise RuntimeError(
                "【未配置 MINERU_API_KEY】系统严格遵循真实调用原则，禁止 Mock 伪造。"
                "请在根目录 .env 文件中配置有效的 MINERU_API_KEY（前往 https://mineru.net/apiManage/docs 获取）。"
            )

        # 若未直接传入云端文件 URL，则上传本地 PDF 至阿里云 OSS 获取公共访问链接
        if not file_url:
            if not Path(file_path).exists():
                raise FileNotFoundError(f"【PDF 解析失败】未找到待解析文件: {file_path}，且未提供云端 file_url。")
            with open(file_path, "rb") as f:
                content_bytes = f.read()
            sha256 = oss_service.calculate_sha256(content_bytes)
            target_oss_path = f"rag_storage/temp_parse/{sha256[:8]}_{Path(file_name).name}"
            file_url = oss_service.upload_file(content_bytes, target_oss_path)

        # 若 Bucket 为私有访问，生成带有签名的临时下载 URL（2 小时有效），确保 MinerU 云端集群能够直接下载
        if "aliyuncs.com" in file_url and "OSSAccessKeyId" not in file_url:
            signed_file_url = oss_service.sign_url(file_url, expires=7200)
        else:
            signed_file_url = file_url

        api_base = settings.MINERU_API_URL.rstrip("/")
        task_endpoint = f"{api_base}/extract/task"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        payload = {
            "url": signed_file_url,
            "model_version": settings.MINERU_MODEL_VERSION or "vlm",
            "is_ocr": True,
            "enable_formula": True,
            "enable_table": True,
            "language": "ch",
            "data_id": doc_id
        }

        # 1. 提交解析任务
        try:
            resp = requests.post(task_endpoint, headers=headers, json=payload, timeout=30)
        except Exception as e:
            raise RuntimeError(f"【MinerU API 任务提交网络请求失败】: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(f"【MinerU API 任务提交失败】HTTP {resp.status_code}: {resp.text}")

        res_data = resp.json()
        if res_data.get("code") != 0:
            raise RuntimeError(
                f"【MinerU API 任务提交失败】code={res_data.get('code')}, msg={res_data.get('msg')}, trace_id={res_data.get('trace_id')}"
            )

        task_id = res_data.get("data", {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"【MinerU API 未返回有效的 task_id】响应内容: {res_data}")

        # 2. 定时轮询任务状态
        query_endpoint = f"{api_base}/extract/task/{task_id}"
        max_wait_seconds = int(getattr(settings, "MINERU_TIMEOUT_SECONDS", 1200))
        poll_interval = 3
        start_time = time.time()
        full_zip_url = None

        while True:
            if time.time() - start_time > max_wait_seconds:
                raise TimeoutError(
                    f"【MinerU API 解析任务超时】task_id={task_id} 超过 {max_wait_seconds} 秒未完成。"
                )

            time.sleep(poll_interval)

            try:
                poll_resp = requests.get(query_endpoint, headers=headers, timeout=20)
            except Exception:
                continue

            if poll_resp.status_code in (401, 403):
                raise RuntimeError(
                    f"【MinerU API 鉴权失败】HTTP {poll_resp.status_code}: 请检查 MINERU_API_KEY 配置与账户额度。响应: {poll_resp.text}"
                )
            elif poll_resp.status_code == 404:
                raise RuntimeError(f"【MinerU API 任务不存在】task_id={task_id} HTTP 404: {poll_resp.text}")
            elif poll_resp.status_code != 200:
                continue

            poll_data = poll_resp.json()
            if poll_data.get("code") != 0:
                raise RuntimeError(
                    f"【MinerU 任务轮询异常】code={poll_data.get('code')}, msg={poll_data.get('msg')}"
                )

            task_info = poll_data.get("data", {})
            state = task_info.get("state")

            if state == "done":
                full_zip_url = task_info.get("full_zip_url")
                break
            elif state == "failed":
                err_msg = task_info.get("err_msg", "未知错误")
                raise RuntimeError(f"【MinerU API 解析失败】task_id={task_id}, 错误原因: {err_msg}")
            elif state in ["pending", "running", "converting", "waiting-file"]:
                elapsed = time.time() - start_time
                parser_mode = getattr(settings, "MINERU_PARSER_MODE", "auto").lower()
                timeout_threshold = int(getattr(settings, "MINERU_V4_QUEUE_TIMEOUT_SECONDS", 30))

                # 当处于排队阶段且模式为 auto 时，排队持续时间超过阈值触发降级
                if state in ["pending", "waiting-file"] and parser_mode == "auto" and elapsed >= timeout_threshold:
                    can_downgrade, reason = self._check_agent_api_eligibility(file_path, file_name)
                    if can_downgrade:
                        logger.warning(
                            f"[MinerU] 官方 v4 集群排队已持续 {elapsed:.1f}s (超过阈值 {timeout_threshold}s)，"
                            f"自动无缝降级切换至 Agent 极速解析通道 (task_id={task_id}, doc_id={doc_id})..."
                        )
                        return self._parse_document_via_agent_api(
                            doc_id, file_path, file_name, file_url, doc_dir, images_dir
                        )
                    else:
                        logger.info(
                            f"[MinerU] v4 集群排队已持续 {elapsed:.1f}s，但文档不满足 Agent 降级条件 ({reason})，继续等待 v4 队列..."
                        )
                continue
            else:
                continue

        if not full_zip_url:
            raise RuntimeError(f"【MinerU API 任务完成但未返回 full_zip_url】task_id={task_id}")

        # 3. 下载解析结果压缩包
        zip_temp_path = doc_dir / f"{doc_id}_mineru_result.zip"
        try:
            zip_resp = requests.get(full_zip_url, timeout=120)
            zip_resp.raise_for_status()
            with open(zip_temp_path, "wb") as f:
                f.write(zip_resp.content)
        except Exception as e:
            raise RuntimeError(f"【MinerU 结果压缩包下载失败】URL: {full_zip_url}, 错误: {e}") from e

        # 4. 解压缩产物
        try:
            with zipfile.ZipFile(zip_temp_path, "r") as zf:
                zf.extractall(doc_dir)
        except Exception as e:
            raise RuntimeError(f"【MinerU 结果解压失败】ZIP 文件损坏或不合法: {e}") from e
        finally:
            if zip_temp_path.exists():
                zip_temp_path.unlink()

        # 5. 规整并校验产出物
        normalized = self._locate_mineru_outputs(doc_dir, doc_id)
        if not normalized:
            raise RuntimeError(
                f"【MinerU 产物校验失败】解压后的目录 {doc_dir} 中未能找到有效的 full.md 或 content_list.json。"
            )

        return normalized

    def _check_agent_api_eligibility(self, file_path: str, file_name: str) -> tuple[bool, str]:
        """检查文档是否满足 Agent 极速接口约束 (<=10MB, PDF <= 20页)"""
        p = Path(file_path) if file_path else None
        if not p or not p.exists() or not p.is_file():
            return False, "待解析文件不存在于本地，无法预检大小与页数，保持 v4 调度"

        file_size_mb = p.stat().st_size / (1024 * 1024)
        if file_size_mb > 10.0:
            return False, f"文件大小 {file_size_mb:.1f}MB > 10MB"

        ext = p.suffix.lower()
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(str(p))
                page_count = len(reader.pages)
                if page_count > 20:
                    return False, f"PDF 页数 {page_count}页 > 20页限制"
            except Exception as e:
                logger.debug(f"检查 PDF 页数失败，允许尝试降级: {e}")

        return True, "满足降级条件"

    def _parse_document_via_agent_api(
        self,
        doc_id: str,
        file_path: str,
        file_name: str,
        file_url: Optional[str],
        doc_dir: Path,
        images_dir: Path
    ) -> Dict[str, Any]:
        """
        调用官方 MinerU Agent 极速解析 API (https://mineru.net/api/v1/agent/parse/url)
        针对轻量文档执行秒级免排队解析，并规整产出 full.md, content_list.json, layout.json。
        严格遵循真实调用准则：无本地 Mock，无物理切图时诚实标注 bbox: None，跳过 VLM 图片理解。
        """
        # 1. 确保云端访问 URL (若未提供则上传至 OSS)
        if not file_url:
            if not Path(file_path).exists():
                raise FileNotFoundError(f"【Agent 解析失败】未找到待解析文件: {file_path}，且未提供云端 file_url。")
            with open(file_path, "rb") as f:
                content_bytes = f.read()
            sha256 = oss_service.calculate_sha256(content_bytes)
            target_oss_path = f"rag_storage/temp_parse/{sha256[:8]}_{Path(file_name).name}"
            file_url = oss_service.upload_file(content_bytes, target_oss_path)

        # 私有 OSS 生成签名 URL
        if "aliyuncs.com" in file_url and "OSSAccessKeyId" not in file_url:
            signed_file_url = oss_service.sign_url(file_url, expires=7200)
        else:
            signed_file_url = file_url

        agent_base = getattr(settings, "MINERU_AGENT_BASE_URL", "https://mineru.net/api/v1/agent").rstrip("/")
        task_endpoint = f"{agent_base}/parse/url"

        headers = {"Content-Type": "application/json"}
        api_key = settings.MINERU_API_KEY.strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "url": signed_file_url,
            "language": "ch",
            "enable_table": True,
            "is_ocr": True,
            "enable_formula": True
        }

        # 2. 提交任务
        try:
            resp = requests.post(task_endpoint, headers=headers, json=payload, timeout=30)
        except Exception as e:
            raise RuntimeError(f"【MinerU Agent API 任务提交网络请求失败】: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(f"【MinerU Agent API 任务提交失败】HTTP {resp.status_code}: {resp.text}")

        res_data = resp.json()
        if res_data.get("code") != 0:
            raise RuntimeError(
                f"【MinerU Agent API 任务提交失败】code={res_data.get('code')}, msg={res_data.get('msg')}, trace_id={res_data.get('trace_id')}"
            )

        task_id = res_data.get("data", {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"【MinerU Agent API 未返回有效的 task_id】响应内容: {res_data}")

        # 3. 轮询状态
        query_endpoint = f"{agent_base}/parse/{task_id}"
        agent_max_wait = 180
        poll_interval = 2
        start_time = time.time()
        markdown_url = None

        while True:
            if time.time() - start_time > agent_max_wait:
                raise TimeoutError(
                    f"【MinerU Agent API 解析任务超时】task_id={task_id} 超过 {agent_max_wait} 秒未完成。"
                )

            time.sleep(poll_interval)

            try:
                poll_resp = requests.get(query_endpoint, headers=headers, timeout=20)
            except Exception:
                continue

            if poll_resp.status_code in (401, 403):
                raise RuntimeError(
                    f"【MinerU Agent API 鉴权失败】HTTP {poll_resp.status_code}: 请检查 MINERU_API_KEY 配置与账户额度。响应: {poll_resp.text}"
                )
            elif poll_resp.status_code == 404:
                raise RuntimeError(f"【MinerU Agent API 任务不存在】task_id={task_id} HTTP 404: {poll_resp.text}")
            elif poll_resp.status_code != 200:
                continue

            poll_data = poll_resp.json()
            if poll_data.get("code") != 0:
                raise RuntimeError(
                    f"【MinerU Agent 任务轮询异常】code={poll_data.get('code')}, msg={poll_data.get('msg')}"
                )

            task_info = poll_data.get("data", {})
            state = task_info.get("state")

            if state == "done":
                markdown_url = task_info.get("markdown_url")
                break
            elif state == "failed":
                err_msg = task_info.get("err_msg", "未知错误")
                raise RuntimeError(f"【MinerU Agent API 解析失败】task_id={task_id}, 错误原因: {err_msg}")
            elif state in ["pending", "running", "waiting-file"]:
                continue
            else:
                continue

        if not markdown_url:
            raise RuntimeError(f"【MinerU Agent API 任务完成但未返回 markdown_url】task_id={task_id}")

        # 4. 下载 Markdown 结果
        full_md_path = doc_dir / "full.md"
        try:
            md_resp = requests.get(markdown_url, timeout=60)
            md_resp.raise_for_status()
            raw_text = md_resp.text
            with open(full_md_path, "w", encoding="utf-8") as f:
                f.write(raw_text)
        except Exception as e:
            raise RuntimeError(f"【MinerU Agent Markdown 下载失败】URL: {markdown_url}, 错误: {e}") from e

        # 5. 规整生成 content_list.json 与 layout.json
        artifacts = self._build_content_list_and_artifacts(raw_text, doc_id, file_name, doc_dir)
        artifacts["parse_channel"] = "agent"
        return artifacts

    def _parse_text_markdown_document(
        self,
        doc_id: str,
        file_path: str,
        file_name: str,
        doc_dir: Path,
        images_dir: Path
    ) -> Dict[str, Any]:
        """解析 Markdown / 纯文本类原生技术文档（严禁伪造像素坐标与虚构切图路径）"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw_text = f.read()
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="gbk") as f:
                raw_text = f.read()
        except Exception as e:
            raise RuntimeError(f"【文本文件读取失败】: {e}") from e

        return self._build_content_list_and_artifacts(raw_text, doc_id, file_name, doc_dir)

    def _build_content_list_and_artifacts(
        self,
        raw_text: str,
        doc_id: str,
        file_name: str,
        doc_dir: Path
    ) -> Dict[str, Any]:
        """
        结构化提取原生 Markdown/文本图元 (Text, Heading, Table, Image, CodeBlock)。
        严格原则：流式文本无像素坐标，bbox 必须为 None，无截图表格 img_path 必须为 None，坚决不伪造数据。
        """
        content_list: List[Dict[str, Any]] = []
        lines = raw_text.splitlines()

        current_heading = ""
        buffer_text: List[str] = []
        buffer_start_line = 1
        in_html_table = False
        in_pipe_table = False
        table_lines: List[str] = []
        table_start_line = 1
        in_code = False
        code_lines: List[str] = []
        code_start_line = 1

        def flush_buffer(end_line: int):
            nonlocal buffer_text, buffer_start_line
            if buffer_text:
                full_block = "\n".join(buffer_text).strip()
                if full_block:
                    content_list.append({
                        "type": "text",
                        "text": full_block,
                        "heading": current_heading,
                        "page_idx": 0,
                        "line_number": buffer_start_line,
                        "line_range": [buffer_start_line, end_line],
                        "bbox": None
                    })
                buffer_text = []

        for l_idx, line in enumerate(lines, start=1):
            # 代码块开始/结束
            if line.strip().startswith("```"):
                if not in_code:
                    flush_buffer(l_idx - 1)
                    in_code = True
                    code_start_line = l_idx
                    code_lines = [line]
                else:
                    code_lines.append(line)
                    in_code = False
                    code_block = "\n".join(code_lines)
                    content_list.append({
                        "type": "code",
                        "text": code_block,
                        "heading": current_heading,
                        "page_idx": 0,
                        "line_number": code_start_line,
                        "line_range": [code_start_line, l_idx],
                        "bbox": None
                    })
                    code_lines = []
                continue

            if in_code:
                code_lines.append(line)
                continue

            # 1. HTML 表格处理 (严格以独立行 <table 开头，以 </table> 结尾)
            if not in_html_table and not in_pipe_table and re.search(r'^\s*<table\b', line, re.IGNORECASE):
                flush_buffer(l_idx - 1)
                in_html_table = True
                table_start_line = l_idx
                table_lines = [line]
                if "</table>" in line.lower():
                    in_html_table = False
                    tbl_raw = "\n".join(table_lines)
                    table_lines = []
                    content_list.append({
                        "type": "table",
                        "table_body": tbl_raw,
                        "heading": current_heading,
                        "page_idx": 0,
                        "line_number": table_start_line,
                        "line_range": [table_start_line, l_idx],
                        "bbox": None,
                        "img_path": None
                    })
                continue
            elif in_html_table:
                table_lines.append(line)
                if "</table>" in line.lower():
                    in_html_table = False
                    tbl_raw = "\n".join(table_lines)
                    table_lines = []
                    content_list.append({
                        "type": "table",
                        "table_body": tbl_raw,
                        "heading": current_heading,
                        "page_idx": 0,
                        "line_number": table_start_line,
                        "line_range": [table_start_line, l_idx],
                        "bbox": None,
                        "img_path": None
                    })
                continue

            # 2. Markdown 管道表格处理 (以 | 开头且以 | 结尾)
            if not in_pipe_table and not in_html_table and line.strip().startswith("|") and line.strip().endswith("|"):
                flush_buffer(l_idx - 1)
                in_pipe_table = True
                table_start_line = l_idx
                table_lines = [line]
                continue
            elif in_pipe_table:
                if line.strip().startswith("|"):
                    table_lines.append(line)
                    continue
                else:
                    in_pipe_table = False
                    # 校验是否为真正合规的 Markdown 表格 (至少2行，且包含表头分隔行 |---|)
                    has_delimiter = any(re.match(r'^\s*\|(?:\s*:?-+:?\s*\|)+\s*$', tl) for tl in table_lines)
                    if len(table_lines) >= 2 and has_delimiter:
                        tbl_raw = "\n".join(table_lines)
                        content_list.append({
                            "type": "table",
                            "table_body": tbl_raw,
                            "heading": current_heading,
                            "page_idx": 0,
                            "line_number": table_start_line,
                            "line_range": [table_start_line, l_idx - 1],
                            "bbox": None,
                            "img_path": None
                        })
                    else:
                        if not buffer_text:
                            buffer_start_line = table_start_line
                        buffer_text.extend(table_lines)
                    table_lines = []
                    # 当前行跳出表格后，继续进入后续检查

            # 标题检测
            if line.strip().startswith("#"):
                flush_buffer(l_idx - 1)
                level = len(line) - len(line.lstrip("#"))
                heading_text = line.lstrip("#").strip()
                current_heading = heading_text
                content_list.append({
                    "type": "heading",
                    "text": heading_text,
                    "text_level": level,
                    "page_idx": 0,
                    "line_number": l_idx,
                    "line_range": [l_idx, l_idx],
                    "bbox": None
                })
                continue

            # 图片标签识别 (支持 Markdown ![alt](url) 与 HTML <img ...> 标签)
            img_match = re.search(r'!\[(.*?)\]\((.*?)\)', line)
            html_img_match = re.search(r'<img\b[^>]*>', line, re.IGNORECASE) if not img_match else None

            if img_match or html_img_match:
                if img_match:
                    alt_text = img_match.group(1)
                    img_src = img_match.group(2)
                    img_start, img_end = img_match.span()
                else:
                    tag_str = html_img_match.group(0)
                    src_m = re.search(r'src=["\']([^"\']+)["\']', tag_str, re.IGNORECASE)
                    alt_m = re.search(r'alt=["\']([^"\']*)["\']', tag_str, re.IGNORECASE)
                    img_src = src_m.group(1) if src_m else ""
                    alt_text = alt_m.group(1) if alt_m else ""
                    img_start, img_end = html_img_match.span()

                # 保留图片同行的前导与后置文字，防止正文信息被静默丢弃
                pre_text = line[:img_start].strip()
                post_text = line[img_end:].strip()

                if pre_text:
                    if not buffer_text:
                        buffer_start_line = l_idx
                    buffer_text.append(pre_text)

                flush_buffer(l_idx)

                content_list.append({
                    "type": "image",
                    "alt": alt_text,
                    "img_path": img_src,
                    "heading": current_heading,
                    "page_idx": 0,
                    "line_number": l_idx,
                    "line_range": [l_idx, l_idx],
                    "bbox": None
                })

                if post_text:
                    buffer_start_line = l_idx
                    buffer_text.append(post_text)

                continue

            if not buffer_text:
                buffer_start_line = l_idx
            buffer_text.append(line)

        if in_pipe_table and table_lines:
            has_delimiter = any(re.match(r'^\s*\|(?:\s*:?-+:?\s*\|)+\s*$', tl) for tl in table_lines)
            if len(table_lines) >= 2 and has_delimiter:
                content_list.append({
                    "type": "table",
                    "table_body": "\n".join(table_lines),
                    "heading": current_heading,
                    "page_idx": 0,
                    "line_number": table_start_line,
                    "line_range": [table_start_line, len(lines)],
                    "bbox": None,
                    "img_path": None
                })
            else:
                if not buffer_text:
                    buffer_start_line = table_start_line
                buffer_text.extend(table_lines)
        elif in_html_table and table_lines:
            content_list.append({
                "type": "table",
                "table_body": "\n".join(table_lines),
                "heading": current_heading,
                "page_idx": 0,
                "line_number": table_start_line,
                "line_range": [table_start_line, len(lines)],
                "bbox": None,
                "img_path": None
            })

        flush_buffer(len(lines))

        # 持久化输出到 data/parsed/{doc_id}/
        full_md_path = doc_dir / "full.md"
        with open(full_md_path, "w", encoding="utf-8") as f:
            f.write(raw_text)

        content_list_path = doc_dir / f"{doc_id}_content_list.json"
        with open(content_list_path, "w", encoding="utf-8") as f:
            json.dump(content_list, f, ensure_ascii=False, indent=2)

        layout_tree = {
            "doc_id": doc_id,
            "file_name": file_name,
            "format": "markdown_text",
            "total_elements": len(content_list),
            "total_lines": len(lines)
        }
        layout_path = doc_dir / "layout.json"
        with open(layout_path, "w", encoding="utf-8") as f:
            json.dump(layout_tree, f, ensure_ascii=False, indent=2)

        return {
            "doc_id": doc_id,
            "doc_dir": str(doc_dir),
            "full_md_path": str(full_md_path),
            "content_list_path": str(content_list_path),
            "layout_path": str(layout_path),
            "content_list": content_list
        }

    def _locate_mineru_outputs(self, doc_dir: Path, doc_id: str) -> Optional[Dict[str, Any]]:
        """
        定位真实 MinerU API / 引擎输出物，并严格按规范规整至根工作空间:
        data/parsed/{doc_id}/
        ├── full.md
        ├── layout.json
        ├── {doc_id}_content_list.json
        └── images/
        """
        json_candidates = list(doc_dir.rglob("*_content_list.json")) + list(doc_dir.rglob("content_list.json"))
        # 路径去重
        json_candidates = list({p.resolve(): p for p in json_candidates}.values())

        md_candidates = [m for m in doc_dir.rglob("*.md") if m.name not in ["full.enhanced.md", "README.md"]]
        md_candidates = list({m.resolve(): m for m in md_candidates}.values())
        md_candidates.sort(key=lambda x: 0 if x.name == "full.md" else 1)

        if not json_candidates or not md_candidates:
            return None

        standard_full_md = doc_dir / "full.md"
        standard_content_list = doc_dir / f"{doc_id}_content_list.json"
        standard_images_dir = doc_dir / "images"
        standard_images_dir.mkdir(parents=True, exist_ok=True)

        best_md = md_candidates[0]
        if best_md.resolve() != standard_full_md.resolve():
            shutil.copy2(best_md, standard_full_md)

        best_json = json_candidates[0]
        with open(best_json, "r", encoding="utf-8") as f:
            cl = json.load(f)

        # 同步各层级子目录中提取的高清切图至 images/ 规范目录
        for img_file in doc_dir.rglob("*.*"):
            if img_file.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"]:
                if img_file.parent.resolve() != standard_images_dir.resolve():
                    dest = standard_images_dir / img_file.name
                    if not dest.exists():
                        shutil.copy2(img_file, dest)

        # 规范化 content_list 中切图相对路径
        for item in cl:
            if item.get("type") in ["image", "table"]:
                p = item.get("img_path")
                if p and not p.startswith("http://") and not p.startswith("https://"):
                    img_name = Path(p).name
                    if (standard_images_dir / img_name).exists():
                        item["img_path"] = f"images/{img_name}"

        # 规范化保存 content_list.json
        with open(standard_content_list, "w", encoding="utf-8") as f:
            json.dump(cl, f, ensure_ascii=False, indent=2)

        # 确保 layout.json 存在
        standard_layout = doc_dir / "layout.json"
        layout_candidates = list(doc_dir.rglob("layout.json")) + list(doc_dir.rglob("*_middle.json")) + list(doc_dir.rglob("middle.json")) + list(doc_dir.rglob("*_model.json"))
        layout_candidates = [l for l in layout_candidates if l.resolve() != standard_layout.resolve()]
        if layout_candidates:
            shutil.copy2(layout_candidates[0], standard_layout)
        elif not standard_layout.exists():
            with open(standard_layout, "w", encoding="utf-8") as f:
                json.dump({"doc_id": doc_id, "total_elements": len(cl), "pages": 1}, f, indent=2)

        return {
            "doc_id": doc_id,
            "doc_dir": str(doc_dir),
            "full_md_path": str(standard_full_md),
            "content_list_path": str(standard_content_list),
            "layout_path": str(standard_layout),
            "content_list": cl,
            "parse_channel": "v4"
        }

mineru_adapter = MinerUParserAdapter()
