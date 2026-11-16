/**
 * 连接器对接文档解析 —— 逐字搬自 admin.html:3243-3710。
 *
 * 为什么不做改写：这段代码是对着真实对接文档调出来的（mcpServers JSON、
 * README 里的 curl、.docx 里的说明、同一份文档里同时给 /mcp 和 /sse 的写法）。
 * 重写只会把踩过的坑再踩一遍，而它本身没有任何 DOM 依赖，可以直接搬。
 *
 * 唯一改动：每个 `function` 前加 `export`，并在文件级关掉类型检查。类型标注
 * 待后续按真实返回值补（analyzeConnectorDoc 的返回结构见 ConnectorAnalysis）。
 */
// @ts-nocheck
export async function readAnyDocText(file) {
  const name = String(file.name || '').toLowerCase();
  if (name.endsWith('.docx')) return docxToText(file);
  if (name.endsWith('.doc')) {
    throw new Error('旧版 .doc 读不了。用 Word 另存为 .docx，或直接复制正文粘贴。');
  }
  if (name.endsWith('.pdf')) {
    throw new Error('PDF 读不了（浏览器里没有可靠的纯前端提取）。复制正文粘贴，或另存为 .txt。');
  }
  const text = await file.text();
  if (!text.trim()) throw new Error('文件里没有文本内容。');
  return text;
}

/* .docx 就是个 zip 里装了一份 word/document.xml。这里只用浏览器原生的
   DecompressionStream 拆中央目录，不引 JSZip——管理台是单文件、离线可用的，
   加一个 CDN 依赖等于给自己造一个"断网就传不了文档"的故障。 */
export async function docxToText(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const xml = await unzipTextEntry(bytes, 'word/document.xml');
  return docxXmlToText(xml);
}

export function _u16(b, i) { return b[i] | (b[i + 1] << 8); }
export function _u32(b, i) {
  return (b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24)) >>> 0;
}

export async function unzipTextEntry(bytes, wanted) {
  if (bytes.length < 22) throw new Error('这个 .docx 是空的或已损坏。');
  const floor = Math.max(0, bytes.length - 65558);   // EOCD 最长 22 + 64KB 注释
  let eocd = -1;
  for (let i = bytes.length - 22; i >= floor; i -= 1) {
    if (_u32(bytes, i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('不是有效的 .docx（找不到 zip 目录）。');
  const total = _u16(bytes, eocd + 10);
  let p = _u32(bytes, eocd + 16);
  for (let n = 0; n < total; n += 1) {
    if (_u32(bytes, p) !== 0x02014b50) break;
    const method = _u16(bytes, p + 10);
    const compSize = _u32(bytes, p + 20);
    const nameLen = _u16(bytes, p + 28);
    const extraLen = _u16(bytes, p + 30);
    const commentLen = _u16(bytes, p + 32);
    const localOff = _u32(bytes, p + 42);
    const name = new TextDecoder().decode(bytes.subarray(p + 46, p + 46 + nameLen));
    if (name === wanted) {
      const lNameLen = _u16(bytes, localOff + 26);
      const lExtraLen = _u16(bytes, localOff + 28);
      const start = localOff + 30 + lNameLen + lExtraLen;
      const data = bytes.subarray(start, start + compSize);
      if (method === 0) return new TextDecoder().decode(data);
      if (method !== 8) throw new Error('这个 .docx 用了不支持的压缩方式。');
      if (typeof DecompressionStream === 'undefined') {
        throw new Error('当前浏览器读不了 .docx，请升级浏览器或改传 .txt。');
      }
      const stream = new Blob([data]).stream()
        .pipeThrough(new DecompressionStream('deflate-raw'));
      return await new Response(stream).text();
    }
    p += 46 + nameLen + extraLen + commentLen;
  }
  throw new Error('.docx 里没找到正文（word/document.xml）。');
}

export function docxXmlToText(xml) {
  return String(xml)
    .replace(/<w:tab\b[^>]*\/>/g, '\t')
    .replace(/<w:br\b[^>]*\/>/g, '\n')
    .replace(/<\/w:tc>/g, '\t')
    .replace(/<\/w:tr>/g, '\n')
    .replace(/<\/w:p>/g, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&')          // 必须最后，否则 &amp;lt; 会被解成 <
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/* ---------- 文本 → 连接条件 ---------- */

/* 一个 MCP 服务端常常同时给几条路：/mcp（streamable http）、/sse、或者一个 npm
   包（stdio）。所以这里返回的不是「一个地址」，而是「一组可选的连接方式 + 鉴权 +
   前置条件」。管理员只需要在里面点一条，不用自己判断哪条能连。 */
export function analyzeConnectorDoc(text) {
  const entry = firstServerEntry(text);
  const obj = (entry && entry.obj) || {};
  const found = {
    source: entry ? 'json' : 'text',
    serverCount: entry ? entry.count : 1,
    name: '',
    description: '',
    transports: [],
    stdio: null,
    auth: { mode: 'none', headers: {}, issuer: '', scopes: [] },
    warnings: [],
  };

  if (entry) {
    found.name = txt(obj.name) || txt(obj.title) || '';
    found.description = txt(obj.description) || txt(obj.desc) || txt(obj.summary) || '';
  }
  // JSON 的 key 通常就是服务真实 id（mcp-html）——比文档里随便一行标题可靠，
  // 所以它排在 docTitle 前面。
  if (!found.name && entry) found.name = entry.key || '';
  if (!found.name) found.name = docTitle(text);
  if (!found.description) found.description = docSummary(text, found.name);

  // 1) JSON 里写死的那一条：type 说了算，没说就看地址后缀
  const jsonUrl = txt(obj.url) || txt(obj.endpoint) || txt(obj.serverUrl)
    || txt(obj.server_url) || txt(obj.baseUrl) || '';
  const jsonType = (txt(obj.type) || txt(obj.transport) || '').toLowerCase();
  if (jsonUrl) {
    addTransport(found, jsonUrl, jsonType === 'sse' ? 'sse' : jsonType ? 'streamable_http' : '');
  }
  // 2) 正文里其它地址。很多文档把两条路并排列出来，只取一个等于丢信息。
  collectUrls(text).forEach((url) => addTransport(found, url, ''));

  // 文档里连名字都没写时，用域名兜一个——比留空让管理员自己想强。
  if (!found.name && found.transports.length) {
    try {
      found.name = new URL(found.transports[0].url).hostname
        .replace(/^(www|mcp|api|app|open|gateway)\./, '').split('.')[0] || '';
    } catch (error) { /* 地址不合法就留空 */ }
  }

  // 3) stdio：JSON 的 command，或正文里的一行启动命令
  const jsonCmd = txt(obj.command);
  if (jsonCmd) {
    const args = Array.isArray(obj.args) ? obj.args.map(String) : [];
    found.stdio = makeStdio(jsonCmd, args, obj.env, txt(obj.cwd));
  } else {
    found.stdio = stdioFromText(text);
  }

  // 4) 鉴权
  let headers = {};
  if (obj.headers && typeof obj.headers === 'object' && !Array.isArray(obj.headers)) {
    for (const [key, val] of Object.entries(obj.headers)) {
      if (typeof val === 'string' && String(key).trim()) headers[String(key).trim()] = val;
    }
  }
  if (!Object.keys(headers).length) headers = pickHeaders(text);

  const im = text.match(
    /["']?(?:issuer|authorization_endpoint|authorize_url|authorization_url)["']?\s*[:=]\s*["']?(https?:\/\/[^\s"',}]+)/i);
  const issuer = txt(obj.oauth_issuer) || (im ? im[1] : '');
  const sm = text.match(/["']?scopes?["']?\s*[:=]\s*(?:"([^"]+)"|'([^']+)'|\[([^\]]+)\])/i);
  const scopes = (sm ? (sm[1] || sm[2] || sm[3] || '') : '').split(/[,\s"']+/)
    .map((s) => s.trim()).filter(Boolean).slice(0, 8);

  if (issuer) {
    // OAuth 和静态头是两条互斥的路：选了前者，后来者不会生效，就别再写进表单。
    found.auth = { mode: 'oauth', headers: {}, issuer, scopes };
    // issuer 不是 MCP 端点。扫描正文时它也会被当成 https 地址收进来，
    // 不过滤就会出现「方法：streamable_http，地址：/oauth/authorize」这种鬼东西。
    found.transports = found.transports.filter((t) => !t.url.startsWith(issuer));
  } else if (Object.keys(headers).length) {
    found.auth = { mode: 'headers', headers, issuer: '', scopes: [] };
  }
  // 授权端点专用的路径（/oauth、/authorize、/token）不是连接地址，无论有没有 issuer
  found.transports = found.transports.filter((t) => !/\/(oauth|authorize|token|login|jwks)\b/i.test(t.url));

  // 5) 前置条件：地址本身能不能从中台所在的机器连出去
  found.transports.forEach((t) => {
    const u = t.url;
    if (/^http:\/\//i.test(u)) {
      found.warnings.push({ k: '安全', tone: 'warn',
        text: '地址是明文 http：中台到对方之间没有 TLS，填在这里的凭据会裸走在网络上。' });
    }
    if (/\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])/i.test(u)) {
      found.warnings.push({ k: '网络', tone: 'warn',
        text: '回环地址只在本机有效：中台跑在服务器上时连不到，换成对方给的对外地址。' });
    } else if (/\/\/(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(u)) {
      found.warnings.push({ k: '网络', tone: 'warn',
        text: '内网地址：确认中台所在机器能路由到这段网络，否则发布后一直连不上。' });
    }
  });
  if (found.auth.mode === 'headers') {
    found.warnings.push({ k: '凭据', text: '静态请求头是全平台共用的一把钥匙；要按人区分授权，改成 OAuth。' });
  }
  if (found.stdio) {
    found.warnings.push({ k: 'stdio', text: '它还能用 stdio 在本地起进程——这条中台不托管，走 MCP_SERVERS。' });
  }
  if (found.serverCount > 1) {
    found.warnings.push({ k: '多服务', text: `文档里有 ${found.serverCount} 个服务，这里只看第一个，其余各建一条。` });
  }
  return found;
}

export function addTransport(found, url, guessed) {
  const clean = String(url || '').replace(/[.,;:!?]+$/, '');
  if (!clean) return;
  // 同一个地址只留一条。JSON 里标了 type 的那条已经在列表里时，
  // 正文里再扫到它就别当成「另一种方式」——那会凭空多出一条连不上的 sse。
  if (found.transports.some((t) => t.url === clean)) return;
  const tp = guessed || (/\/sse\/?(?:["'`\s)]|$)/i.test(clean) ? 'sse' : 'streamable_http');
  const exist = found.transports.find((t) => t.transport === tp);
  // 同一种方式出现多次时留分最高的：文档里常把示例域名和真地址混在一起。
  if (exist) {
    if (scoreUrl(clean) > scoreUrl(exist.url)) exist.url = clean;
    return;
  }
  found.transports.push({ transport: tp, url: clean });
}

export function makeStdio(command, args, env, cwd) {
  let cmd = String(command || '').trim();
  let argv = (args || []).map(String);
  // 粘进来的命令常常是整串（"npx -y @foo/bar"），拆开再存
  if (cmd && /\s/.test(cmd)) {
    const parts = cmd.split(/\s+/).filter(Boolean);
    cmd = parts[0];
    if (!argv.length) argv = parts.slice(1);
  }
  if (!cmd) return null;
  const pkg = argv.find((a) => !a.startsWith('-')) || '';
  return {
    command: cmd,
    args: argv,
    env: (env && typeof env === 'object' && !Array.isArray(env)) ? env : {},
    cwd: cwd || '',
    pkg,
    runtime: runtimeHint(cmd, pkg),
  };
}

export function runtimeHint(cmd, pkg) {
  const base = String(cmd || '').split('/').pop();
  if (base === 'npx' || base === 'node' || base === 'pnpx' || base === 'bunx') {
    return base === 'npx' ? `需要 Node.js（首次运行会从 npm 拉 ${pkg || '包'}）` : '需要 Node.js，包要先装在机器上';
  }
  if (base === 'uvx' || base === 'uv') return `需要 uv（首次运行会从 PyPI 拉 ${pkg || '包'}）`;
  if (/^python3?$/.test(base)) return '需要 Python 环境，依赖要先装好';
  if (base === 'docker') return '需要 Docker';
  return '';
}

/* 正文里那行启动命令。只认开头就是解释器的行，避免把示例里的
   "安装：npm i -g xxx" 当成启动命令。 */
export function stdioFromText(text) {
  const lines = String(text).split('\n').map((l) => l.replace(/^[\s$>`*_#\-]+/, '').trim());
  const hit = lines.find((l) => /^(npx|uvx|pnpx|bunx|node|python3?|deno|docker)\s+\S/.test(l)
    && !/^(npx|pnpx|bunx)\s+(create|init)\b/.test(l));
  if (!hit) return null;
  const tokens = [];
  for (const part of hit.split(/\s+/)) {
    // 命令后面常跟一句人话（"# 启动服务" / "（需要 token）"），到那儿就停
    if (!part || /[\u4e00-\u9fa5]/.test(part) || part.startsWith('#') || part === '|' || part === '&&') break;
    tokens.push(part.replace(/[,;]+$/, ''));
  }
  if (tokens.length < 2) return null;
  return makeStdio(tokens[0], tokens.slice(1), null, '');
}

/* 编辑已有连接器时，把库里那条配置当成「分析结果」展示：
   面板结构一样，只是来源写「已有配置」，不会因为没上传文档就空着。 */
export function analysisFromExisting(c) {
  const transports = [];
  if (c.transport && c.transport !== 'stdio' && c.url) {
    transports.push({ transport: c.transport === 'sse' ? 'sse' : 'streamable_http', url: c.url });
  }
  const keys = Object.keys(c.headers || {});
  return {
    source: 'saved',
    serverCount: 1,
    name: c.name || '',
    description: c.description || '',
    transports,
    stdio: c.transport === 'stdio' && c.command
      ? makeStdio(c.command, c.args || [], c.env, c.cwd)
      : null,
    auth: c.auth === 'oauth'
      ? { mode: 'oauth', headers: {}, issuer: c.oauth_issuer || '', scopes: c.oauth_scopes || [] }
      : { mode: keys.length ? 'headers' : 'none', headers: c.headers || {}, issuer: '', scopes: [] },
    warnings: [],
  };
}

export function txt(value) { return typeof value === 'string' ? value.trim() : ''; }

export function firstServerEntry(text) {
  for (const chunk of jsonChunks(text)) {
    let data;
    try { data = JSON.parse(chunk); } catch (error) { continue; }
    const rows = pickServerEntries(data);
    if (rows && rows.length) {
      return { key: rows[0].key, obj: rows[0].obj, count: rows.length };
    }
  }
  return null;
}

export function jsonChunks(text) {
  const out = [];
  const fence = /```[a-zA-Z0-9_+-]*[ \t]*\n([\s\S]*?)```/g;
  let m;
  while ((m = fence.exec(text))) out.push(m[1].trim());
  out.push(text.trim());
  const a = text.indexOf('{');
  const b = text.lastIndexOf('}');
  if (a >= 0 && b > a) out.push(text.slice(a, b + 1));
  return out.filter(Boolean);
}

export function pickServerEntries(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const wrap = (map) => {
    if (!map || typeof map !== 'object' || Array.isArray(map)) return null;
    const rows = Object.entries(map)
      .filter(([, v]) => v && typeof v === 'object' && !Array.isArray(v))
      .map(([key, obj]) => ({ key, obj }));
    // 裸 JSON 兜底必须确认"像服务端"：否则一份 package.json 也会被当成连接器填进来
    const looksLikeServer = rows.some(({ obj }) => obj.url || obj.command
      || obj.type || obj.transport || obj.endpoint);
    return looksLikeServer ? rows : null;
  };
  if (data.mcpServers) return wrap(data.mcpServers);
  if (data.mcp && data.mcp.servers) return wrap(data.mcp.servers);
  if (data.servers) return wrap(data.servers);
  if (data.url || data.command) {
    return [{ key: txt(data.name) || txt(data.id) || '', obj: data }];
  }
  return wrap(data);
}

const _URL_RE = /https?:\/\/[^\s"'`<>)\]，。、；]+/g;

export function scoreUrl(raw) {
  const url = raw.toLowerCase();
  let score = 0;
  if (/\/sse\/?$/.test(url)) score += 55;
  else if (/\/mcp\/?$/.test(url)) score += 50;
  else if (/\/mcp\b/.test(url)) score += 40;
  else if (/\/message\/?$/.test(url)) score += 30;
  if (url.startsWith('https://')) score += 10;
  // 文档里最常见的是"看我们的仓库 / 看 npm 包"这类链接，它们不是 MCP 端点
  if (/github\.com|gitlab\.com|npmjs\.com|pypi\.org|modelcontextprotocol\.io|docs?\.|blog\./.test(url)) score -= 45;
  // 把网页另存成 .html 再传的情况：href 里的静态资源会被一起扫进来
  if (/\.(css|js|mjs|png|jpe?g|gif|svg|ico|woff2?|ttf|map)(\?|#|$)/.test(url)) score -= 60;
  if (/fonts\.googleapis|cdn\.|unpkg\.com|jsdelivr/.test(url)) score -= 50;
  if (/localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]/.test(url)) score -= 25;
  if (/example\.com|your-|xxx|placeholder|<|>/.test(url)) score -= 30;
  return score;
}

/* 文档里所有「像 MCP 端点」的地址。仓库链接、示例域名、静态资源都被
   scoreUrl 打到 0 分以下，这里一并滤掉——留着它们只会多出两条假地址。 */
export function collectUrls(text) {
  const out = [];
  let m;
  _URL_RE.lastIndex = 0;
  while ((m = _URL_RE.exec(text))) {
    const url = m[0].replace(/[.,;:!?]+$/, '');
    if (scoreUrl(url) <= 0) continue;
    if (!out.includes(url)) out.push(url);
  }
  return out.slice(0, 8);
}

export function cleanLine(value) {
  return String(value).replace(/[*_`#>]/g, '').replace(/\s+/g, ' ').trim().slice(0, 80);
}

export function docTitle(text) {
  const plain = text.replace(/```[\s\S]*?```/g, '');
  const heading = plain.match(/^\s{0,3}#{1,2}\s+(.{2,60})$/m);
  if (heading) return cleanLine(heading[1]);
  const fm = plain.match(/^\s*(?:name|title)\s*[:=]\s*(.{2,60})$/mi);
  if (fm) return cleanLine(fm[1]);
  const first = plain.split('\n').map((line) => line.trim())
    .find((line) => line && line.length <= 40
      && !/^[{}<>[\]*|`#"'@]/.test(line)     // JSON 键、引用、代码、@scope 都不是标题
      && !/[。；，、]|\.$/.test(line)          // 一句话不是标题
      && !/[{}]$/.test(line)
      && !/https?:\/\//.test(line));
  return first ? cleanLine(first) : '';
}

export function docSummary(text, title) {
  const lines = text.split('\n').map((line) => line.trim());
  const buf = [];
  let seenHeading = false;
  for (const line of lines) {
    if (!line) { if (buf.length) break; continue; }
    if (/^#{1,6}\s/.test(line)) {
      if (seenHeading && buf.length) break;
      seenHeading = true;
      continue;
    }
    if (/^```/.test(line)) break;
    // 文档开头那行往往就是标题本身，别把它当简介再填一遍
    if (title && cleanLine(line) === cleanLine(title)) continue;
    // `issuer: https://…` 这种是配置行，不是一句人话
    if (/^[A-Za-z][A-Za-z0-9 _-]{0,23}\s*[:=]\s*\S/.test(line)) {
      if (buf.length) break;
      continue;
    }
    if (/^[-*>|]/.test(line) || /^\d+[.)]/.test(line)) { if (buf.length) break; continue; }
    if (/https?:\/\//.test(line) || /[{}]/.test(line)) { if (buf.length) break; continue; }
    buf.push(line);
    if (buf.join(' ').length > 180) break;
  }
  const summary = buf.join(' ').replace(/\s+/g, ' ').trim();
  return summary.length >= 6 ? summary.slice(0, 200) : '';
}

export function pickHeaders(text) {
  const out = {};
  const re = /(authorization|x-api-key|api[_-]?key|x-auth-token|x-api-token|access[_-]?token)\s*["']?\s*[:=]\s*["']?(Bearer\s+[A-Za-z0-9._~+/=-]{8,}|[A-Za-z0-9._~+/=-]{8,})/gi;
  let m;
  while ((m = re.exec(text))) {
    const key = canonHeader(m[1]);
    if (!out[key]) out[key] = m[2];
  }
  return out;
}

export function canonHeader(raw) {
  const key = raw.toLowerCase().replace(/_/g, '-');
  if (key === 'authorization' || key === 'access-token') return 'Authorization';
  if (key === 'api-key' || key === 'apikey' || key === 'x-api-key') return 'X-API-Key';
  if (key === 'x-auth-token') return 'X-Auth-Token';
  if (key === 'x-api-token') return 'X-API-Token';
  return raw;
}

export function connectorIdFrom(name, url) {
  let host = '';
  try { host = new URL(url).hostname.toLowerCase(); } catch (error) { host = ''; }
  // 纯 IP 当 id 没有任何信息量：42.194.146.23 会生出 "42"，
  // 工具名就变成 42__xxx，排在工具列表里谁也认不出是什么。
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host) || host.includes(':')) host = '';
  // mcp.example.com / api.foo.io → example / foo：这些前缀是协议习惯，不是品牌名
  const fromHost = host.replace(/^(www|mcp|api|app|open|gateway)\./, '').split('.')[0] || '';
  const fromName = String(name || '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  // 名字比域名更能说明这是谁：中文名里的英文部分（MCP HTML 部署服务 → mcp-html）
  // 比 42 这种从 IP 掉下来的碎片有用得多。
  let id = (fromName || fromHost || '').replace(/-{2,}/g, '-').slice(0, 64);
  id = id.replace(/__+/g, '_').replace(/^[._-]+|[._-]+$/g, '');
  if (id && !/^[a-z0-9]/.test(id)) id = 'connector-' + id;
  return id.replace(/[^a-z0-9._-]/g, '');
}

const _ICON_RULES = [
  [/画布|canvas|design|设计|figma/i, '🎨'],
  [/视频|video|剪辑|成片/i, '🎬'],
  [/图片|图像|image|绘图|photo/i, '🖼️'],
  [/地图|map|geo|定位|地址/i, '🗺️'],
  [/文档|doc|word|笔记|note|知识/i, '📄'],
  [/表格|sheet|excel|报表|数据/i, '📊'],
  [/邮件|mail|smtp|通知/i, '✉️'],
  [/代码|code|git|仓库|repo/i, '💻'],
  [/搜索|search|检索|爬/i, '🔎'],
  [/支付|pay|订单|结算/i, '💳'],
  [/客户|crm|销售|线索/i, '🤝'],
];

export function guessIcon() {
  const text = Array.prototype.join.call(arguments, ' ').trim();
  if (!text) return '🔗';
  for (const [re, icon] of _ICON_RULES) if (re.test(text)) return icon;
  return '🔗';
}

