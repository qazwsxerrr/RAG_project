"""src/rag_kb/utils/oss.py —— 阿里云 OSS 统一工具：全项目的文件上传与签名都走这里。

提供 get_client()、build_key() / build_document_key()、upload_file() / upload_bytes() 与 signed_url()；
遵守「bucket 私有读、库里只存 object key 不存 URL」的约定，需要展示时再现场签发带有效期的临时 URL。
严格遵循 Fail-Fast 原则，不提供任何本地静态回退或伪造 URL。
"""

import os
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any
import oss2
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from rag_kb.core.config import settings

retry_oss_network = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((
        oss2.exceptions.RequestError,
        oss2.exceptions.ServerError,
        ConnectionResetError,
        TimeoutError
    )),
    reraise=True
)

class AliyunOSSService:
    """阿里云 OSS 真实对象存储管理封装"""

    def __init__(self):
        self.access_key_id = settings.OSS_ACCESS_KEY_ID.strip()
        self.access_key_secret = settings.OSS_ACCESS_KEY_SECRET.strip()
        self.endpoint = settings.OSS_ENDPOINT.strip()
        self.bucket_name = settings.OSS_BUCKET_NAME.strip()
        self._bucket: Optional[oss2.Bucket] = None

    def get_client(self) -> oss2.Bucket:
        """获取或初始化原生的 oss2.Bucket 客户端"""
        if self._bucket is not None:
            return self._bucket

        if not self.access_key_id or "*" in self.access_key_id:
            raise RuntimeError("【阿里云 OSS 错误】未配置有效的 OSS_ACCESS_KEY_ID，严禁本地降级！")
        if not self.access_key_secret or "*" in self.access_key_secret:
            raise RuntimeError("【阿里云 OSS 错误】未配置有效的 OSS_ACCESS_KEY_SECRET，严禁本地降级！")
        if not self.bucket_name or "*" in self.bucket_name:
            raise RuntimeError("【阿里云 OSS 错误】未配置有效的 OSS_BUCKET_NAME，严禁本地降级！")

        auth = oss2.Auth(self.access_key_id, self.access_key_secret)
        self._bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)
        return self._bucket

    @staticmethod
    def calculate_sha256(content: bytes) -> str:
        """计算二进制字节流的 SHA-256 哈希字符串"""
        return hashlib.sha256(content).hexdigest()

    def build_key(self, prefix: str, file_name: str, doc_id: Optional[str] = None) -> str:
        """构造结构化的 OSS Object Key"""
        clean_prefix = prefix.strip("/")
        if doc_id:
            return f"{clean_prefix}/{doc_id}/{file_name}"
        return f"{clean_prefix}/{file_name}"

    def build_document_key(self, department: str, sha256: str, file_name: str) -> str:
        """构造原始文档归档 key"""
        dept_clean = department.strip("/").replace("/", "_") or "general"
        return f"{settings.OSS_KEY_PREFIX}/raw/{dept_clean}/{sha256[:8]}_{file_name}"

    def upload_bytes(self, file_bytes: bytes, target_key: str, content_type: Optional[str] = None) -> str:
        """上传二进制内容到 OSS，返回生成的标准 object key"""
        bucket = self.get_client()
        clean_key = target_key.lstrip("/")

        headers = {}
        if content_type:
            headers["Content-Type"] = content_type

        @retry_oss_network
        def _put():
            return bucket.put_object(clean_key, file_bytes, headers=headers if headers else None)

        res = _put()
        if res.status != 200:
            raise RuntimeError(f"【OSS 上传失败】HTTP 状态码: {res.status}, key: {clean_key}")

        return clean_key

    def upload_file(self, file_path_or_bytes: Any, target_key: str, content_type: Optional[str] = None) -> str:
        """上传文件（支持本地路径或 bytes）到 OSS"""
        if isinstance(file_path_or_bytes, bytes):
            return self.upload_bytes(file_path_or_bytes, target_key, content_type)
        
        path = Path(file_path_or_bytes)
        if not path.exists():
            raise FileNotFoundError(f"【OSS 上传错误】待上传本地文件不存在: {path}")
        with open(path, "rb") as f:
            data = f.read()
        return self.upload_bytes(data, target_key, content_type)

    def signed_url(self, target_key_or_url: str, expires: Optional[int] = None) -> str:
        """为指定的 OSS Object Key 生成带有效期的临时访问 URL（默认 2 小时）"""
        bucket = self.get_client()
        clean_endpoint = self.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        base_prefix = f"https://{self.bucket_name}.{clean_endpoint}/"
        http_prefix = f"http://{self.bucket_name}.{clean_endpoint}/"

        clean_key = target_key_or_url
        if clean_key.startswith(base_prefix):
            clean_key = clean_key[len(base_prefix):]
        elif clean_key.startswith(http_prefix):
            clean_key = clean_key[len(http_prefix):]
        clean_key = clean_key.lstrip("/")

        exp = expires or settings.OSS_SIGNED_URL_EXPIRES
        return bucket.sign_url("GET", clean_key, exp)

    def sign_url(self, target_key_or_url: str, expires: Optional[int] = None) -> str:
        """signed_url 的别名方法"""
        return self.signed_url(target_key_or_url, expires)

    def get_object_stream(self, target_path_or_url: str, chunk_size: int = 65536):
        """
        从阿里云 OSS 纯内存分块流式拉取对象，返回二进制生成器、Content-Length 与干净路径。
        零磁盘临时文件落盘，严格遵守 AGENTS.md 准则。
        """
        bucket = self.get_client()
        clean_endpoint = self.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        base_prefix = f"https://{self.bucket_name}.{clean_endpoint}/"
        http_prefix = f"http://{self.bucket_name}.{clean_endpoint}/"

        clean_key = target_path_or_url
        if clean_key.startswith(base_prefix):
            clean_key = clean_key[len(base_prefix):]
        elif clean_key.startswith(http_prefix):
            clean_key = clean_key[len(http_prefix):]
        clean_key = clean_key.lstrip("/")

        oss_obj = bucket.get_object(clean_key)
        if oss_obj.status != 200:
            raise RuntimeError(f"【阿里云 OSS 读取失败】HTTP 状态码: {oss_obj.status}, 路径: {clean_key}")

        def stream_generator():
            try:
                while True:
                    chunk = oss_obj.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                oss_obj.close()

        return stream_generator(), oss_obj.content_length, clean_key

    def probe_connection(self) -> Dict[str, Any]:
        """真实探测并验证阿里云 OSS 连通性、读写权限"""
        bucket = self.get_client()
        probe_key = f"{settings.OSS_KEY_PREFIX}/.probe_health_check.txt"
        probe_content = b"Aliyun OSS connectivity probe OK"

        # 写入探测
        put_res = bucket.put_object(probe_key, probe_content)
        if put_res.status != 200:
            raise RuntimeError(f"OSS 写入探测失败: {put_res.status}")

        # 读取探测
        get_res = bucket.get_object(probe_key)
        assert get_res.read() == probe_content, "读取内容与探测写入不一致"

        # 清理探测对象
        bucket.delete_object(probe_key)

        return {
            "status": "success",
            "bucket": self.bucket_name,
            "endpoint": self.endpoint
        }

oss_service = AliyunOSSService()

# 模块顶层函数快捷导出
def get_client() -> oss2.Bucket:
    return oss_service.get_client()

def build_key(prefix: str, file_name: str, doc_id: Optional[str] = None) -> str:
    return oss_service.build_key(prefix, file_name, doc_id)

def build_document_key(department: str, sha256: str, file_name: str) -> str:
    return oss_service.build_document_key(department, sha256, file_name)

def upload_file(file_path_or_bytes: Any, target_key: str, content_type: Optional[str] = None) -> str:
    return oss_service.upload_file(file_path_or_bytes, target_key, content_type)

def upload_bytes(file_bytes: bytes, target_key: str, content_type: Optional[str] = None) -> str:
    return oss_service.upload_bytes(file_bytes, target_key, content_type)

def signed_url(target_key_or_url: str, expires: Optional[int] = None) -> str:
    return oss_service.signed_url(target_key_or_url, expires)
