/**
 * Markdown 子集渲染器。
 *
 * 设计原因：不引入 marked / DOMPurify。
 *   1. 模型输出属于不可信内容；使用 innerHTML 渲染需额外 sanitizer。
 *      本实现直接生成 DOM 节点，文本统一通过 textContent 写入，从结构上消除注入面。
 *   2. 项目保持零前端依赖。
 *
 * 支持：标题、段落、粗体/斜体/行内代码、围栏代码块、无序/有序列表、引用、
 *       分隔线、GFM 表格与链接（仅允许 http/https/mailto/站内相对路径）。
 *
 * 分为两段：parseMarkdown() 为纯函数（可在 node 中单元测试），
 * renderMarkdown() 负责将解析结果转换为 DOM。
 */

export type Inline =
  | { type: "text"; text: string }
  | { type: "code"; text: string }
  | { type: "strong"; children: Inline[] }
  | { type: "em"; children: Inline[] }
  | { type: "link"; href: string; children: Inline[] };

export type Block =
  | { type: "paragraph"; children: Inline[] }
  | { type: "heading"; level: number; children: Inline[] }
  | { type: "code"; lang: string; text: string }
  | { type: "list"; ordered: boolean; items: Inline[][] }
  | { type: "blockquote"; blocks: Block[] }
  | { type: "hr" }
  | { type: "table"; header: Inline[][]; rows: Inline[][][] };

/** 只允许这几种链接，避免 javascript: / data: 之类。 */
export function safeHref(raw: string): string | null {
  const href = raw.trim();
  if (!href) return null;
  if (/^(https?:|mailto:)/i.test(href)) return href;
  if (href.startsWith("/") || href.startsWith("#") || href.startsWith("./") || href.startsWith("../")) {
    return href;
  }
  return null;
}

const CODE_SPAN = /`([^`]+)`/;
const LINK = /\[([^\]]*)\]\(([^)\s]+)\)/;
const STRONG = /\*\*(.+?)\*\*/
const EM = /(^|[^*_])(\*([^*]+)\*|_([^_]+)_)/;

/** 行内解析：行内代码 > 链接 > 粗体 > 斜体 > 纯文本，逐段递归。 */
export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let rest = text;

  while (rest) {
    const candidates: { index: number; kind: string; match: RegExpExecArray }[] = [];
    for (const [kind, re] of [
      ["code", CODE_SPAN],
      ["link", LINK],
      ["strong", STRONG],
      ["em", EM],
    ] as const) {
      const m = re.exec(rest);
      if (m) candidates.push({ index: m.index, kind, match: m });
    }
    if (candidates.length === 0) {
      out.push({ type: "text", text: rest });
      break;
    }
    candidates.sort((a, b) => a.index - b.index);
    const { index, kind, match } = candidates[0];
    if (index > 0) out.push({ type: "text", text: rest.slice(0, index) });

    if (kind === "code") {
      out.push({ type: "code", text: match[1] });
      rest = rest.slice(index + match[0].length);
    } else if (kind === "link") {
      const href = safeHref(match[2]);
      if (href) out.push({ type: "link", href, children: parseInline(match[1]) });
      else out.push({ type: "text", text: match[0] });   // 不安全的链接按原样显示
      rest = rest.slice(index + match[0].length);
    } else if (kind === "strong") {
      out.push({ type: "strong", children: parseInline(match[1]) });
      rest = rest.slice(index + match[0].length);
    } else {
      const prefix = match[1] ?? "";                     // EM 正则可能吃掉前导字符
      const inner = match[3] ?? match[4] ?? "";
      if (prefix) out.push({ type: "text", text: prefix });
      out.push({ type: "em", children: parseInline(inner) });
      rest = rest.slice(index + match[0].length);
    }
  }
  return mergeAdjacentText(out);
}

/** 相邻的文本节点合并成一个，避免碎成好几段。 */
export function mergeAdjacentText(nodes: Inline[]): Inline[] {
  const out: Inline[] = [];
  for (const node of nodes) {
    const last = out[out.length - 1];
    if (node.type === "text" && last && last.type === "text") {
      out[out.length - 1] = { type: "text", text: last.text + node.text };
    } else {
      out.push(node);
    }
  }
  return out;
}

const HEADING = /^(#{1,6})\s+(.*)$/;
const HR = /^\s*([-*_])(?:\s*\1){2,}\s*$/;
const UL_ITEM = /^\s*[-*+]\s+(.*)$/;
const OL_ITEM = /^\s*\d+[.)]\s+(.*)$/;
const QUOTE = /^>\s?(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/;

function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

/** 块级解析：逐行扫，遇到新的块类型就收尾上一个。 */
export function parseMarkdown(text: string): Block[] {
  const lines = (text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    // 围栏代码块
    const fence = /^\s*```(\w*)\s*$/.exec(line);
    if (fence) {
      const lang = fence[1] ?? "";
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;                              // 跳过结束围栏
      blocks.push({ type: "code", lang, text: body.join("\n") });
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, children: parseInline(heading[2]) });
      i += 1;
      continue;
    }

    if (HR.test(line)) {
      blocks.push({ type: "hr" });
      i += 1;
      continue;
    }

    // 引用：连续的 > 行收成一个块，内部再递归解析
    if (QUOTE.test(line)) {
      const inner: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        inner.push(QUOTE.exec(lines[i])![1]);
        i += 1;
      }
      blocks.push({ type: "blockquote", blocks: parseMarkdown(inner.join("\n")) });
      continue;
    }

    // 表格：至少两行，第二行是分隔行
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const header = splitRow(line).map(parseInline);
      i += 2;
      const rows: Inline[][][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitRow(lines[i]).map(parseInline));
        i += 1;
      }
      blocks.push({ type: "table", header, rows });
      continue;
    }

    // 列表
    if (UL_ITEM.test(line) || OL_ITEM.test(line)) {
      const ordered = OL_ITEM.test(line);
      const re = ordered ? OL_ITEM : UL_ITEM;
      const items: Inline[][] = [];
      while (i < lines.length && re.test(lines[i])) {
        items.push(parseInline(re.exec(lines[i])![1]));
        i += 1;
        // 缩进的续行并进当前条目
        const extra: string[] = [];
        while (i < lines.length && /^\s+\S/.test(lines[i]) && !UL_ITEM.test(lines[i]) && !OL_ITEM.test(lines[i])) {
          extra.push(lines[i].trim());
          i += 1;
        }
        if (extra.length) items[items.length - 1].push({ type: "text", text: " " + extra.join(" ") });
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }

    // 段落：连续非空、且不是其它块开头的行
    const para: string[] = [];
    while (i < lines.length && lines[i].trim()) {
      const next = lines[i];
      if (para.length > 0 && (HEADING.test(next) || HR.test(next) || /^\s*```/.test(next) || QUOTE.test(next))) {
        break;
      }
      para.push(next);
      i += 1;
    }
    blocks.push({ type: "paragraph", children: parseInline(para.join(" ").trim()) });
  }

  return blocks;
}

/* ------------------------------------------------------------------ DOM 渲染 */

function renderInline(nodes: Inline[], doc: Document): DocumentFragment {
  const frag = doc.createDocumentFragment();
  for (const node of nodes) {
    if (node.type === "text") {
      frag.append(doc.createTextNode(node.text));
    } else if (node.type === "code") {
      frag.append(doc.createElement("code"));
      (frag.lastChild as HTMLElement).textContent = node.text;
      (frag.lastChild as HTMLElement).className = "md-code";
    } else if (node.type === "strong") {
      const el = doc.createElement("strong");
      el.append(renderInline(node.children, doc));
      frag.append(el);
    } else if (node.type === "em") {
      const el = doc.createElement("em");
      el.append(renderInline(node.children, doc));
      frag.append(el);
    } else {
      const a = doc.createElement("a");
      a.setAttribute("href", node.href);
      a.setAttribute("rel", "noreferrer noopener");
      a.setAttribute("target", "_blank");
      a.append(renderInline(node.children, doc));
      frag.append(a);
    }
  }
  return frag;
}

/** 把 Markdown 渲染成 DOM（安全：不使用 innerHTML）。 */
export function renderMarkdown(text: string, doc: Document = document): DocumentFragment {
  return renderBlocks(parseMarkdown(text), doc);
}

/** 渲染一组块。引用块内部会递归调用它。 */
export function renderBlocks(blocks: Block[], doc: Document = document): DocumentFragment {
  const frag = doc.createDocumentFragment();

  for (const block of blocks) {
    if (block.type === "heading") {
      const el = doc.createElement(`h${Math.min(6, Math.max(1, block.level))}`);
      el.append(renderInline(block.children, doc));
      frag.append(el);
    } else if (block.type === "paragraph") {
      const el = doc.createElement("p");
      el.append(renderInline(block.children, doc));
      frag.append(el);
    } else if (block.type === "code") {
      const pre = doc.createElement("pre");
      const code = doc.createElement("code");
      if (block.lang) code.className = `lang-${block.lang}`;
      code.textContent = block.text;
      pre.append(code);
      frag.append(pre);
    } else if (block.type === "list") {
      const el = doc.createElement(block.ordered ? "ol" : "ul");
      for (const item of block.items) {
        const li = doc.createElement("li");
        li.append(renderInline(item, doc));
        el.append(li);
      }
      frag.append(el);
    } else if (block.type === "blockquote") {
      const el = doc.createElement("blockquote");
      el.append(renderBlocks(block.blocks, doc));
      frag.append(el);
    } else if (block.type === "hr") {
      frag.append(doc.createElement("hr"));
    } else {
      const table = doc.createElement("table");
      const thead = doc.createElement("thead");
      const tr = doc.createElement("tr");
      for (const cell of block.header) {
        const th = doc.createElement("th");
        th.append(renderInline(cell, doc));
        tr.append(th);
      }
      thead.append(tr);
      table.append(thead);
      const tbody = doc.createElement("tbody");
      for (const row of block.rows) {
        const r = doc.createElement("tr");
        for (const cell of row) {
          const td = doc.createElement("td");
          td.append(renderInline(cell, doc));
          r.append(td);
        }
        tbody.append(r);
      }
      table.append(tbody);
      frag.append(table);
    }
  }
  return frag;
}
