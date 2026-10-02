import os
import hashlib
from typing import Optional, Dict, Any
import oss2
from backend.app.core.config import settings

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

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
    """
    阿里云 OSS 真实对象存储服务（严格接入模式，不进行本地存储回退）
    要求必须在 .env 中正确配置:
      - OSS_ACCESS_KEY_ID
      - OSS_ACCESS_KEY_SECRET
      - OSS_ENDPOINT
      - OSS_BUCKET_NAME
    """

    def __init__(self):
        self.access_key_id = settings.OSS_ACCESS_KEY_ID.strip()
        self.access_key_secret = settings.OSS_ACCESS_KEY_SECRET.strip()
        self.endpoint = settings.OSS_ENDPOINT.strip()
        self.bucket_name = settings.OSS_BUCKET_NAME.strip()
        self._bucket = None

        # 确保阿里云 OSS 流量不被本地科学上网/代理中间件误拦截导致 SSL EOF
        for env_var in ["NO_PROXY", "no_proxy"]:
            current = os.environ.get(env_var, "")
            if "aliyuncs.com" not in current:
                os.environ[env_var] = f"{current},aliyuncs.com" if current else "aliyuncs.com"

    def _ensure_bucket(self) -> oss2.Bucket:
        """确保已初始化真实的 oss2.Bucket 实例"""
        if self._bucket is not None:
            return self._bucket

        # 校验配置有效性
        if not self.access_key_id or self.access_key_id.startswith("your_") or "*" in self.access_key_id:
            raise ValueError(
                "【阿里云 OSS 接入错误】: 未配置有效的 OSS_ACCESS_KEY_ID，请在 .env 中填入真实的完整 AccessKey ID！"
            )
        if not self.access_key_secret or self.access_key_secret.startswith("your_") or "*" in self.access_key_secret:
            raise ValueError(
                "【阿里云 OSS 接入错误】: 未配置有效的 OSS_ACCESS_KEY_SECRET，请在 .env 中填入真实的完整 AccessKey Secret！"
            )
        if not self.bucket_name or self.bucket_name.startswith("your_") or "*" in self.bucket_name:
            raise ValueError(
                "【阿里云 OSS 接入错误】: 未配置有效的 OSS_BUCKET_NAME，请在 .env 中填入您在阿里云控制台创建的 Bucket 名称！"
            )

        auth = oss2.Auth(self.access_key_id, self.access_key_secret)
        self._bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)
        return self._bucket

    def upload_file(self, file_bytes: bytes, target_path: str, content_type: Optional[str] = None) -> str:
        """
        上传文件二进制字节流至阿里云 OSS，返回真实的云端 CDN/访问 URL。
        受 3 次指数退避微重试保护，若最终失败坚决抛出真实异常以便排错。
        """
        bucket = self._ensure_bucket()
        clean_path = target_path.lstrip("/")

        headers = {}
        if content_type:
            headers["Content-Type"] = content_type

        @retry_oss_network
        def _put_with_retry():
            return bucket.put_object(clean_path, file_bytes, headers=headers if headers else None)

        try:
            result = _put_with_retry()
        except Exception as e:
            raise RuntimeError(f"【阿里云 OSS 上传失败】(已重试 3 次) 路径: {clean_path}, 错误: {e}") from e

        if result.status != 200:
            raise RuntimeError(f"【阿里云 OSS 上传失败】HTTP 状态码: {result.status}, 路径: {clean_path}")

        # 规范化 endpoint URL
        clean_endpoint = self.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        cloud_url = f"https://{self.bucket_name}.{clean_endpoint}/{clean_path}"
        return cloud_url

    def download_file(self, target_path_or_url: str) -> bytes:
        """
        从阿里云 OSS 真实拉取文件二进制数据（用于断点续跑时本地缺失资产自愈）。
        """
        bucket = self._ensure_bucket()
        clean_endpoint = self.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        base_prefix = f"https://{self.bucket_name}.{clean_endpoint}/"
        http_prefix = f"http://{self.bucket_name}.{clean_endpoint}/"

        clean_key = target_path_or_url
        if clean_key.startswith(base_prefix):
            clean_key = clean_key[len(base_prefix):]
        elif clean_key.startswith(http_prefix):
            clean_key = clean_key[len(http_prefix):]
        clean_key = clean_key.lstrip("/")

        @retry_oss_network
        def _get_with_retry():
            return bucket.get_object(clean_key)

        try:
            oss_obj = _get_with_retry()
            data = oss_obj.read()
            oss_obj.close()
            return data
        except Exception as e:
            raise RuntimeError(f"【阿里云 OSS 读取失败】路径: {clean_key}, 错误: {e}") from e

    def download_file_text(self, target_path_or_url: str, encoding: str = "utf-8") -> str:
        """从阿里云 OSS 读取并解码文本文件"""
        raw_bytes = self.download_file(target_path_or_url)
        return raw_bytes.decode(encoding)

    def sign_url(self, target_path_or_url: str, expires: int = 7200) -> str:
        """
        为 OSS 对象生成带签名的临时授权访问 URL (默认有效 2 小时)，
        确保私有 Bucket 的对象也能被外部云端服务（如 MinerU API）正常拉取。
        """
        bucket = self._ensure_bucket()
        clean_endpoint = self.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        base_prefix = f"https://{self.bucket_name}.{clean_endpoint}/"
        http_prefix = f"http://{self.bucket_name}.{clean_endpoint}/"

        clean_key = target_path_or_url
        if clean_key.startswith(base_prefix):
            clean_key = clean_key[len(base_prefix):]
        elif clean_key.startswith(http_prefix):
            clean_key = clean_key[len(http_prefix):]
        clean_key = clean_key.lstrip("/")

        return bucket.sign_url("GET", clean_key, expires)

    def get_object_stream(self, target_path_or_url: str, chunk_size: int = 65536):
        """
        从阿里云 OSS 纯内存分块流式拉取对象，返回二进制生成器、Content-Length 与干净路径。
        零磁盘临时文件落盘，严格遵守 AGENTS.md 准则。
        """
        bucket = self._ensure_bucket()
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
        """
        真实探测并验证阿里云 OSS 连通性、权限及 Bucket 状态
        """
        bucket = self._ensure_bucket()
        
        # 1. 尝试获取 Bucket 基础信息
        try:
            info = bucket.get_bucket_info()
            bucket_location = info.location
            bucket_storage_class = info.storage_class
        except Exception as e:
            # 兼容有些 RAM 子用户没有 GetBucketInfo 权限但有 PutObject 权限的情况
            bucket_location = "未知/受限"
            bucket_storage_class = "未知"

        # 2. 上传一个微小的健康探测对象，验证写权限
        probe_key = "rag_storage/.probe_health_check.txt"
        probe_content = b"Aliyun OSS connectivity probe OK"
        put_result = bucket.put_object(probe_key, probe_content)
        
        if put_result.status != 200:
            raise RuntimeError(f"OSS 写权限探测失败，状态码: {put_result.status}")

        # 3. 验证读权限
        get_result = bucket.get_object(probe_key)
        read_bytes = get_result.read()
        assert read_bytes == probe_content, "读取探测数据内容不一致"

        # 4. 清理探测对象
        bucket.delete_object(probe_key)

        clean_endpoint = self.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        return {
            "status": "success",
            "message": "阿里云 OSS 连通性验证成功！读写权限正常。",
            "bucket_name": self.bucket_name,
            "endpoint": self.endpoint,
            "location": bucket_location,
            "storage_class": bucket_storage_class,
            "sample_url_prefix": f"https://{self.bucket_name}.{clean_endpoint}/rag_storage/"
        }

    @staticmethod
    def calculate_sha256(content: bytes) -> str:
        """计算文件内容的 SHA-256 校验和"""
        return hashlib.sha256(content).hexdigest()

oss_service = AliyunOSSService()

if __name__ == "__main__":
    import sys
    print("正在测试阿里云 OSS 连通性...")
    try:
        res = oss_service.probe_connection()
        print(f"✅ {res['message']}")
        print(f"Bucket: {res['bucket_name']}")
        print(f"Endpoint: {res['endpoint']}")
        print(f"URL 前缀: {res['sample_url_prefix']}")
    except Exception as err:
        print(f"❌ 阿里云 OSS 探测失败: {err}")
        sys.exit(1)
