/* ==================================================================
   设计层脚本（taste-skill · paper & ink）
   ------------------------------------------------------------------
   只做三件事，不改任何业务逻辑：
     1. 尽早把 data-theme 落到 <html>（避免首帧白闪）；
     2. 往页头插一个主题切换按钮，选择记在 localStorage；
     3. 展示层收口：把裸露的 u_xxxx 换成昵称、把 default 租户说成人话。

   第 3 条是补丁，不是正解——正解是接口直接给 display_name / tenant_name。
   补丁集中在 tidyIdentity() 里，接口改了就能整段删掉。
   ================================================================== */
(function () {
  'use strict';

  var KEY = 'taste-theme';
  var root = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function readPref() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function systemTheme() { return mq && mq.matches ? 'dark' : 'light'; }
  function resolve(pref) {
    return (pref === 'light' || pref === 'dark') ? pref : systemTheme();
  }
  function paint(pref) {
    var theme = resolve(pref);
    root.setAttribute('data-theme', theme);
    root.style.colorScheme = theme;
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'dark' ? '#0e1518' : '#f7f9fa');
  }

  // ---- 1. 首帧就定下来 ----
  var pref = readPref() || 'auto';
  paint(pref);

  if (mq) {
    var onSystem = function () { if ((readPref() || 'auto') === 'auto') paint('auto'); };
    if (mq.addEventListener) mq.addEventListener('change', onSystem);
    else if (mq.addListener) mq.addListener(onSystem);
  }

  // ---- 2. 切换按钮 ----
  var SUN = '<svg class="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 3.2v2.1M12 18.7v2.1M3.2 12h2.1M18.7 12h2.1M5.9 5.9l1.5 1.5M16.6 16.6l1.5 1.5M18.1 5.9l-1.5 1.5M7.4 16.6l-1.5 1.5"/></svg>';
  var MOON = '<svg class="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.4A8.2 8.2 0 0 1 9.6 4a8.6 8.6 0 1 0 10.4 10.4z"/></svg>';

  // 各页面的页头结构不一样，按优先级找第一个存在的落点。
  // admin 的 .who 是账号信息，按钮插到它前面；其它都是追加到动作区末尾。
  var SLOTS = [
    { sel: '#topActions', mode: 'append' },
    { sel: '.top-links', mode: 'append' },
    { sel: '.top-right', mode: 'append' },
    { sel: '#who', mode: 'before' },
    { sel: '.pane-head .actions', mode: 'append' },
    { sel: '.pane-head', mode: 'append' }
  ];

  function makeButton() {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'taste-theme-btn';
    btn.setAttribute('aria-label', '切换深浅色');
    btn.innerHTML = SUN + MOON;
    btn.addEventListener('click', function () {
      var next = resolve(readPref() || 'auto') === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(KEY, next); } catch (e) {}
      paint(next);
      sync();
    });
    return btn;
  }

  function sync() {
    var btn = document.querySelector('.taste-theme-btn');
    if (!btn) return;
    var dark = root.getAttribute('data-theme') === 'dark';
    btn.title = dark ? '切换到浅色' : '切换到深色';
    btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
  }

  function mountButton() {
    if (document.querySelector('.taste-theme-btn')) { sync(); return; }
    for (var i = 0; i < SLOTS.length; i++) {
      var slot = document.querySelector(SLOTS[i].sel);
      if (!slot) continue;
      var btn = makeButton();
      if (SLOTS[i].mode === 'before') slot.parentNode.insertBefore(btn, slot);
      else slot.appendChild(btn);
      sync();
      return;
    }
  }

  // ---- 3. 展示层收口 ----
  var nameCache = null;

  function fetchName(cb) {
    if (nameCache !== null) return cb(nameCache);
    fetch('/api/me', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (me) {
        nameCache = (me && me.display_name) || '';
        cb(nameCache);
      })
      .catch(function () { nameCache = ''; cb(''); });
  }

  // default 是内部租户标识，直接摆给用户看像是没填完的字段
  function humanTenant(text) {
    return text.replace(/(租户\s+|·\s*)default\b/g, '$1默认空间');
  }

  function tidyIdentity() {
    // 管理控制台页头：u_xxxxxxxx → 昵称
    var who = document.getElementById('who');
    if (who) {
      var mono = who.querySelector('.mono');
      if (mono && /^u_[0-9a-f]{10,}$/i.test(mono.textContent.trim())) {
        fetchName(function (name) {
          if (name) mono.textContent = name;
          else mono.textContent = mono.textContent.trim().slice(0, 6) + '…';
        });
      }
      var muted = who.querySelector('.muted');
      if (muted) muted.textContent = humanTenant(muted.textContent);
    }
    // 主界面侧栏：'成员 · default'
    var sub = document.getElementById('userSub');
    if (sub) {
      var t = humanTenant(sub.textContent);
      if (t !== sub.textContent) sub.textContent = t;
    }
  }

  function boot() {
    mountButton();
    tidyIdentity();
    // #who / #userSub 都是 JS 后填的，晚一拍再看一次
    setTimeout(tidyIdentity, 900);
    setTimeout(tidyIdentity, 2400);
    if (window.MutationObserver) {
      var pending = false;
      var mo = new MutationObserver(function () {
        if (pending) return;
        pending = true;
        requestAnimationFrame(function () { pending = false; tidyIdentity(); });
      });
      ['who', 'userSub'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) mo.observe(el, { childList: true, subtree: true, characterData: true });
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
