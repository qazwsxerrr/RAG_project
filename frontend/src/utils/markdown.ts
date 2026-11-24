import MarkdownIt from 'markdown-it';

/** 创建禁用模糊域名自动链接的 MarkdownIt 实例，防止 xxx.md 文件名被误转为外链 */
export function createMarkdownRenderer(options?: { html?: boolean }): MarkdownIt {
  const md = new MarkdownIt({
    html: options?.html ?? false,
    linkify: true,
    breaks: true,
  });
  md.linkify.set({ fuzzyLink: false, fuzzyEmail: false });
  return md;
}

const inlineMd = createMarkdownRenderer({ html: false });

/** 安全渲染后端表格 HTML 单元格（th/td）内的行内 Markdown 标记（如 **加粗**、`code`） */
export function renderTableCellsMarkdown(tableHtml?: string): string {
  if (!tableHtml) return '';
  if (typeof DOMParser === 'undefined') return tableHtml;

  const doc = new DOMParser().parseFromString(tableHtml, 'text/html');
  doc.querySelectorAll('script, iframe, object, embed, style').forEach((el) => el.remove());
  doc.querySelectorAll('*').forEach((el) => {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith('on') || (name === 'href' && /^\s*javascript:/i.test(attr.value))) {
        el.removeAttribute(attr.name);
      }
    }
  });

  doc.querySelectorAll('th, td').forEach((cell) => {
    // 仅处理纯文本或不含复杂块元素的单元格文本节点，保留原有结构标签
    const walker = doc.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    const textNodes: Text[] = [];
    let current = walker.nextNode();
    while (current) {
      const parentTag = current.parentElement?.tagName.toLowerCase();
      if (parentTag !== 'code' && parentTag !== 'pre' && current.nodeValue && /[*_`~[\]]/.test(current.nodeValue)) {
        textNodes.push(current as Text);
      }
      current = walker.nextNode();
    }
    for (const node of textNodes) {
      const raw = node.nodeValue || '';
      const html = inlineMd.renderInline(raw);
      if (html !== raw) {
        const span = doc.createElement('span');
        span.innerHTML = html;
        node.replaceWith(...Array.from(span.childNodes));
      }
    }
  });

  return doc.body.innerHTML;
}
