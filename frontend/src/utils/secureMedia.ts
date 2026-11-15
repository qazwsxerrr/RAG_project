import { ref } from 'vue';
import { useAuthStore, isTokenExpired } from '../stores/auth';
import { documentsApi } from '../api/documents';

/**
 * 使用内存态 Authorization: Bearer 请求受保护的后端图片资源并生成局部 ObjectURL，
 * 彻底杜绝将通用业务 JWT 拼入 URL 查询参数。
 */
export async function fetchAuthorizedObjectUrl(
  url: string,
  signal?: AbortSignal
): Promise<string> {
  const authStore = useAuthStore();
  let currentToken = authStore.token;
  if (!currentToken || isTokenExpired(currentToken)) {
    currentToken = await authStore.refreshToken();
  }

  const doFetch = (tk: string) =>
    fetch(url, {
      headers: { Authorization: `Bearer ${tk}` },
      signal,
    });

  let resp = await doFetch(currentToken);
  if (resp.status === 401) {
    const refreshed = await authStore.refreshToken();
    resp = await doFetch(refreshed);
  }

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const errJson = await resp.json();
      detail = errJson.detail || detail;
    } catch {
      // ignore non-json body
    }
    throw new Error(detail);
  }

  const blob = await resp.blob();
  return URL.createObjectURL(blob);
}

/**
 * 组件级受控图片 ObjectURL 缓存池：按源 URL 去重并发拉取，卸载或翻页时一键释放全部 Blob 内存。
 */
export function createSecureBlobCache() {
  const blobMap = ref<Record<string, string>>({});
  const inflight = new Map<string, Promise<string>>();
  let activeController = new AbortController();

  const ensureLoaded = (rawUrl?: string | null): Promise<string> => {
    if (!rawUrl || !rawUrl.startsWith('/api/')) {
      return Promise.resolve(rawUrl || '');
    }
    if (blobMap.value[rawUrl]) {
      return Promise.resolve(blobMap.value[rawUrl]);
    }
    const existing = inflight.get(rawUrl);
    if (existing) {
      return existing;
    }

    const controller = activeController;
    const p = fetchAuthorizedObjectUrl(rawUrl, controller.signal)
      .then((objUrl) => {
        if (controller.signal.aborted) {
          URL.revokeObjectURL(objUrl);
          return '';
        }
        blobMap.value = { ...blobMap.value, [rawUrl]: objUrl };
        return objUrl;
      })
      .finally(() => {
        inflight.delete(rawUrl);
      });

    inflight.set(rawUrl, p);
    return p;
  };

  const getUrl = (rawUrl?: string | null): string => {
    if (!rawUrl) return '';
    if (!rawUrl.startsWith('/api/')) return rawUrl;
    return blobMap.value[rawUrl] || '';
  };

  const revokeAll = () => {
    activeController.abort();
    activeController = new AbortController();
    inflight.clear();
    for (const objUrl of Object.values(blobMap.value)) {
      if (objUrl.startsWith('blob:')) {
        URL.revokeObjectURL(objUrl);
      }
    }
    blobMap.value = {};
  };

  return {
    ensureLoaded,
    getUrl,
    revokeAll,
  };
}

/**
 * 在新标签页安全打开小体积切图原图（使用一次性受控 ObjectURL，关闭后延迟回收）
 */
export async function openProtectedImageInNewTab(url?: string | null): Promise<void> {
  if (!url) return;
  const popup = window.open('about:blank', '_blank');
  try {
    const blobUrl = url.startsWith('blob:') ? url : await fetchAuthorizedObjectUrl(url);
    if (popup) {
      popup.location.replace(blobUrl);
    } else {
      window.open(blobUrl, '_blank', 'noopener,noreferrer');
    }
    if (!url.startsWith('blob:')) {
      setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
    }
  } catch (err: any) {
    if (popup) popup.close();
    alert(`加载图片失败: ${err?.message || '请检查访问权限'}`);
  }
}

/**
 * 在新标签页使用 60 秒单文档受限短票打开原生流式 PDF/文档预览（完整保留 HTTP Range 206 分片秒开）
 */
export async function openDocumentPreviewWithTicket(
  docId: string,
  pageAnchor?: number
): Promise<void> {
  if (!docId) return;
  const popup = window.open('about:blank', '_blank');
  try {
    const res = await documentsApi.createStreamTicket(docId, 'preview');
    const hash = pageAnchor && pageAnchor > 0 ? `#page=${pageAnchor}` : '';
    const targetUrl = `/api/v1/documents/${encodeURIComponent(docId)}/preview?ticket=${encodeURIComponent(res.ticket)}${hash}`;
    if (popup) {
      popup.location.replace(targetUrl);
    } else {
      window.open(targetUrl, '_blank', 'noopener,noreferrer');
    }
  } catch (err: any) {
    if (popup) popup.close();
    alert(`打开文档预览失败: ${err?.message || '无权访问该文档'}`);
  }
}
