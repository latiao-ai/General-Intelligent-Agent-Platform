/**
 * 技能包读取（.zip / 目录 / SKILL.md） —— 逐字搬自 admin.html:3999-4031 与 4070-4250。
 */
// @ts-nocheck
/* ---------- 图标：缩到 128px 再转 data URL ---------- */
export function iconFileToDataURL(file, size = 128) {
  return new Promise((resolve, reject) => {
    if (!file) { reject(new Error('没有选到文件')); return; }
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读不到这个文件'));
    const isSvg = /svg/i.test(file.type) || /\.svg$/i.test(file.name);
    if (isSvg) {
      if (file.size > 120 * 1024) { reject(new Error('SVG 超过 120KB，换个小一点的')); return; }
      reader.onload = () => resolve(String(reader.result));
      reader.readAsDataURL(file);
      return;
    }
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error('这张图打不开'));
      img.onload = () => {
        const w = img.naturalWidth || size;
        const h = img.naturalHeight || size;
        const side = Math.min(w, h);
        // 取中间那块正方形，非方图不会被拉变形。
        const canvas = document.createElement('canvas');
        canvas.width = size;
        canvas.height = size;
        canvas.getContext('2d').drawImage(
          img, Math.max(0, (w - side) / 2), Math.max(0, (h - side) / 2), side, side, 0, 0, size, size);
        resolve(canvas.toDataURL('image/png'));
      };
      img.src = String(reader.result);
    };
    reader.readAsDataURL(file);
  });
}


/* ---------- frontmatter / zip / 目录 ---------- */
export function readYamlMeta(raw) {
  const meta = {};
  String(raw || '').split(/\r?\n/).forEach((line) => {
    const at = line.indexOf(':');
    if (at <= 0) return;
    const key = line.slice(0, at).trim().toLowerCase();
    const value = line.slice(at + 1).trim().replace(/^["']|["']$/g, '');
    if (key && value) meta[key] = value;
  });
  // 同一份 SKILL.md 里常有 display_name / description_zh：中文名、中文说明优先。
  if (meta.display_name_zh || meta.display_name) {
    meta.name = meta.display_name_zh || meta.display_name;
  }
  if (meta.description_zh) meta.description = meta.description_zh;
  return meta;
}

// 兼容三种写法：① 文件开头就是 frontmatter（标准技能包）；② frontmatter 被整段
// 贴进了 ```markdown 代码块（「全文合并」文档就这样，开头是目录不是 ---）；
// ③ 正文中间某个 --- 块里写着 name。只认开头的话，合并文档一个字段都读不出来。
export function parseSkillFrontmatter(text) {
  const raw = String(text || '');
  const head = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*\r?\n?/.exec(raw);
  if (head) return { meta: readYamlMeta(head[1]), body: raw.slice(head[0].length) };

  // 代码块里的 SKILL.md：正文只取这个块，别把合并文档里的脚本、参考文档也当成指令。
  const fence = /```(?:markdown|md)?[ \t]*\r?\n(---\r?\n[\s\S]*?)\r?\n```/.exec(raw);
  if (fence) {
    const inner = fence[1];
    const hit = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*\r?\n?/.exec(inner);
    if (hit && /(^|\n)\s*(name|display_name|description)\s*:/i.test(hit[1])) {
      return { meta: readYamlMeta(hit[1]), body: inner.slice(hit[0].length) };
    }
  }

  const loose = /(?:^|\n)---\r?\n([\s\S]{0,2000}?)\r?\n---[ \t]*(?=\r?\n)/g;
  let hit;
  while ((hit = loose.exec(raw)) !== null) {
    if (/(^|\n)\s*(name|display_name)\s*:/i.test(hit[1])) {
      return { meta: readYamlMeta(hit[1]), body: raw.slice(hit.index + hit[0].length) };
    }
  }
  return { meta: {}, body: raw };
}

/* 这里只需要「从一个包里读出一份 SKILL.md」，为这点需求引一整个解压库不划算：
   自己翻 zip 的中央目录，deflate 的条目丢给浏览器自带的 DecompressionStream。 */
export async function zipIndex(buffer) {
  const dv = new DataView(buffer);
  let eocd = -1;
  for (let i = buffer.byteLength - 22; i >= 0 && i >= buffer.byteLength - 66000; i -= 1) {
    if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('不是一个有效的 zip');
  const total = dv.getUint16(eocd + 10, true);
  let offset = dv.getUint32(eocd + 16, true);
  const entries = [];
  for (let i = 0; i < total; i += 1) {
    if (dv.getUint32(offset, true) !== 0x02014b50) break;
    const nameLen = dv.getUint16(offset + 28, true);
    const extraLen = dv.getUint16(offset + 30, true);
    const commentLen = dv.getUint16(offset + 32, true);
    entries.push({
      name: new TextDecoder('utf-8').decode(new Uint8Array(buffer, offset + 46, nameLen)),
      method: dv.getUint16(offset + 10, true),
      size: dv.getUint32(offset + 20, true),
      head: dv.getUint32(offset + 42, true),
    });
    offset += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

export async function zipReadText(buffer, entry) {
  const dv = new DataView(buffer);
  const nameLen = dv.getUint16(entry.head + 26, true);
  const extraLen = dv.getUint16(entry.head + 28, true);
  const start = entry.head + 30 + nameLen + extraLen;
  const payload = new Uint8Array(buffer, start, entry.size);
  let bytes = payload;
  if (entry.method === 8) {
    const stream = new Blob([payload]).stream()
      .pipeThrough(new DecompressionStream('deflate-raw'));
    bytes = new Uint8Array(await new Response(stream).arrayBuffer());
  } else if (entry.method !== 0) {
    throw new Error('这个压缩包的压缩方式不支持，重新打成 zip 再试');
  }
  return new TextDecoder('utf-8').decode(bytes);
}

export function depthOf(path) {
  return String(path || '').split(/[\\/]/).length;
}

// 拖进来的文件夹要递归摊平：DataTransfer.files 对目录是空的。
export function readDirEntry(entry) {
  return new Promise((resolve) => {
    if (!entry) { resolve([]); return; }
    if (entry.isFile) {
      entry.file((f) => resolve([f]), () => resolve([]));
      return;
    }
    const reader = entry.createReader();
    const all = [];
    const step = () => reader.readEntries((batch) => {
      if (!batch.length) {
        Promise.all(all.map(readDirEntry)).then((groups) => resolve(groups.flat()));
        return;
      }
      all.push(...batch);
      step();
    }, () => resolve([]));
    step();
  });
}

// 描述太短或没写的时候，概述补上正文里标题下面那段导语。只扫到第一个二级标题
// 为止，免得把「## 何时使用」的整段正文当成概述。
export function introParagraph(text) {
  const buf = [];
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trim();
    if (/^##\s/.test(line)) break;
    if (!line) { if (buf.length) break; continue; }
    if (/^#\s/.test(line)) continue;
    if (/^([-*+]|\d+[.)])\s|^>|^```|^\|/.test(line)) { if (buf.length) break; continue; }
    buf.push(line);
  }
  return buf.join(' ').replace(/\s{2,}/g, ' ').trim();
}

export function overviewFromSkill(meta, body) {
  const desc = String(meta.description || '').replace(/\s+/g, ' ').trim();
  const intro = introParagraph(body);
  if (!desc) return intro;
  if (desc.length >= 30 || !intro) return desc;
  // 两句拼一起，中间得有个句号，否则「…纪要 会议开完…」读起来是断的。
  const glue = /[。！？.!?:：；;]$/.test(desc) ? '' : '。';
  return (desc + glue + intro).trim();
}

// 包里没有 name 的时候，用文件名兜底：meeting-notes.zip → meeting notes。
export function nameFromSource(source) {
  const first = String(source || '').split('›')[0].trim();
  const base = first.split(/[\\/]/).pop() || '';
  return base.replace(/\.(zip|md|markdown)$/i, '').replace(/[-_]+/g, ' ').trim();
}
