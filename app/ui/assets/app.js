/* =========================================================
   瀑布相册 · fnPhotowall 前端逻辑
   ========================================================= */
(function () {
  'use strict';

  /* ---------------- 基础 ---------------- */
  const baseEl = document.querySelector('base');
  const BASE = (baseEl ? new URL(baseEl.href, location.href).pathname
                       : location.pathname).replace(/\/+$/, '');

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const rand = () => Math.floor(Math.random() * 1e9);

  function api(path) {
    return fetch(BASE + path, { headers: { 'Accept': 'application/json' } })
      .then(r => r.json().then(j => {
        if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      }));
  }
  function withTimeout(promise, ms, msg) {
    return Promise.race([
      promise,
      new Promise((_, rej) => setTimeout(() => rej(new Error(msg || '请求超时')), ms))
    ]);
  }
  function post(path, body) {
    return fetch(BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(r => r.json().then(j => {
      if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
      return j;
    }));
  }
  const fileUrl = p => BASE + '/api/file?path=' + encodeURIComponent(p);
  const thumbUrl = (p, w, v) => BASE + '/api/thumb?path=' + encodeURIComponent(p) + '&w=' + (w || 480) + '&v=' + (v || 0);

  function fmtSize(n) {
    if (!n && n !== 0) return '';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = n, i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + ' ' + u[i];
  }
  function fmtDuration(s) {
    if (!s) return '';
    s = Math.round(s);
    const m = Math.floor(s / 60);
    const ss = String(s % 60).padStart(2, '0');
    return m + ':' + ss;
  }

  /* ---------------- Toast / Modal ---------------- */
  const toastEl = $('#toast');
  let toastTimer = 0;
  function toast(msg, isError) {
    toastEl.textContent = msg;
    toastEl.classList.toggle('is-error', !!isError);
    toastEl.classList.add('is-show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('is-show'), 2200);
  }

  const modalEl = $('#modal');
  let modalResolve = null;
  function confirmBox(title, html, okText) {
    $('#modalTitle').textContent = title;
    $('#modalText').innerHTML = html;
    $('#modalOk').textContent = okText || '确认';
    modalEl.classList.add('is-open');
    modalEl.setAttribute('aria-hidden', 'false');
    return new Promise(resolve => { modalResolve = resolve; });
  }
  function closeModal(result) {
    modalEl.classList.remove('is-open');
    modalEl.setAttribute('aria-hidden', 'true');
    if (modalResolve) { modalResolve(result); modalResolve = null; }
  }
  $('#modalCancel').addEventListener('click', () => closeModal(false));
  $('#modalOk').addEventListener('click', () => closeModal(true));
  modalEl.addEventListener('click', e => { if (e.target === modalEl) closeModal(false); });

  /* ---------------- 瀑布流布局 ---------------- */
  class Masonry {
    constructor(el, opts) {
      this.el = el;
      this.opts = Object.assign({ minCol: 280, maxCols: 6, gap: 12 }, opts || {});
      this.cards = [];
      this.el.classList.add('measuring');
      this._timer = 0;
      window.addEventListener('resize', () => this.schedule(), { passive: true });
      if (window.ResizeObserver) {
        this._ro = new ResizeObserver(() => this.schedule());
        this._ro.observe(el);
      }
      requestAnimationFrame(() => this.el.classList.remove('measuring'));
    }
    get gap() {
      const w = window.innerWidth || 1440;
      return w <= 720 ? 9 : this.opts.gap;
    }
    columns(width) {
      const w = window.innerWidth || width;
      let base = this.opts.minCol;
      if (w <= 480) base = 152;
      else if (w <= 720) base = 176;
      else if (w <= 1080) base = 238;
      return clamp(Math.floor((width + this.gap) / (base + this.gap)) || 1, 1, this.opts.maxCols);
    }
    clear() {
      this.cards = [];
      this.el.innerHTML = '';
      this.el.style.height = '0px';
    }
    add(card) { this.cards.push(card); }
    schedule() {
      if (this._timer) return;
      this._timer = requestAnimationFrame(() => { this._timer = 0; this.layout(); });
      // 兜底：iframe 未显示时 rAF 不跑，150ms 后强制排一次
      setTimeout(() => { if (this._timer) { this._timer = 0; this.layout(); } }, 150);
    }
    layout() {
      const W = this.el.clientWidth;
      if (!W) {
        // 容器还没布局好（例如 iframe 刚创建、父窗口隐藏），稍后重试
        if (this.cards.length && !this._retry) {
          this._retry = setTimeout(() => { this._retry = 0; this.layout(); }, 200);
        }
        return;
      }
      this._retry = 0;
      if (!this.cards.length) { this.el.style.height = '0px'; return; }
      const cols = this.columns(W);
      const colW = (W - this.gap * (cols - 1)) / cols;
      const heights = new Array(cols).fill(0);
      const order = [];
      for (let i = 0; i < this.cards.length; i++) {
        let k = 0;
        for (let j = 1; j < cols; j++) if (heights[j] < heights[k] - 0.5) k = j;
        const r = clamp(this.cards[i].ratio || 1.3333, 0.45, 2.2);
        const h = Math.round(colW / r);
        const c = this.cards[i];
        c.x = Math.round(k * (colW + this.gap));
        c.y = Math.round(heights[k]);
        c.w = colW;
        c.h = h;
        order.push(c);
        heights[k] += h + this.gap;
      }
      for (let i = 0; i < order.length; i++) {
        const c = order[i];
        c.el.style.width = c.w + 'px';
        c.el.style.height = c.h + 'px';
        c.el.style.transform = 'translate3d(' + c.x + 'px,' + c.y + 'px,0)';
      }
      this.el.style.height = Math.max(0, Math.max.apply(null, heights) - this.gap) + 'px';
    }
  }

  /* ---------------- 卡片 ---------------- */
  const HEART = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor"><path d="M12 20s-7-4.35-7-9.5A4.5 4.5 0 0 1 12 7.5 4.5 4.5 0 0 1 19 10.5c0 5.15-7 9.5-7 9.5z"/></svg>';
  const PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5z"/></svg>';

  function makeCard(item, small, priority) {
    const el = document.createElement('div');
    el.className = 'card' + (item.kind === 'video' ? ' ph-video' : '');
    el.dataset.path = item.path;

    const img = document.createElement('img');
    img.className = 'thumb';
    img.loading = priority ? 'eager' : 'lazy';
    img.decoding = 'async';
    if (priority && 'fetchPriority' in img) img.fetchPriority = 'high';
    img.alt = item.name || '';
    el.appendChild(img);

    if (item.kind === 'video') {
      const pb = document.createElement('div');
      pb.className = 'play-badge';
      pb.innerHTML = PLAY;
      el.appendChild(pb);
      const badge = document.createElement('div');
      badge.className = 'card-badge';
      badge.textContent = item.duration ? fmtDuration(item.duration) : '视频';
      el.appendChild(badge);
    }
    if (item.fav) {
      const h = document.createElement('div');
      h.className = 'card-heart';
      h.innerHTML = HEART;
      el.appendChild(h);
    }
    const cap = document.createElement('div');
    cap.className = 'card-cap';
    cap.innerHTML = (item.fav ? '<span class="cap-fav">♥</span>' : '') +
      '<span class="cap-name"></span>';
    cap.querySelector('.cap-name').textContent = item.name;
    el.appendChild(cap);

    const ratio = (item.w && item.h) ? item.w / item.h : 0;
    const card = { el, ratio: ratio || (item.kind === 'video' ? 1.6 : 1.33), item, img };

    img.addEventListener('load', () => {
      img.classList.add('loaded');
      el.classList.add('is-loaded');
      if (!ratio && img.naturalWidth && img.naturalHeight) {
        card.ratio = img.naturalWidth / img.naturalHeight;
        if (card.grid) card.grid.schedule();
      }
    }, { once: true });
    img.addEventListener('error', () => {
      img.classList.add('loaded');
      el.classList.add('is-loaded');
      img.style.opacity = '1';
      el.style.background = 'linear-gradient(150deg,#1b1e26,#101219)';
      if (!ratio) { card.ratio = item.kind === 'video' ? 1.6 : 1.33; if (card.grid) card.grid.schedule(); }
      if (item.kind === 'video') img.removeAttribute('src');
    });

    img.src = thumbUrl(item.path, small ? 320 : 480, item.mtime);
    return card;
  }

  function refreshCardHeart(path, fav) {
    $$('.card').forEach(el => {
      if (el.dataset.path !== path) return;
      let heart = el.querySelector('.card-heart');
      const capFav = el.querySelector('.cap-fav');
      if (fav && !heart) {
        heart = document.createElement('div');
        heart.className = 'card-heart';
        heart.innerHTML = HEART;
        el.appendChild(heart);
      } else if (!fav && heart) {
        heart.remove();
      }
      if (capFav) capFav.style.display = fav ? '' : 'none';
    });
  }

  /* ---------------- 状态 ---------------- */
  let ultraOpen = false;      // 应用内超清层是否打开（提前声明，供各函数安全引用）
  const state = {
    view: 'photo',
    feeds: {
      photo: { kind: 'image', items: [], offset: 0, total: 0, loading: false, done: false, seed: rand() },
      video: { kind: 'video', items: [], offset: 0, total: 0, loading: false, done: false, seed: rand() }
    },
    folder: { path: '', stack: [''], index: 0, data: null, loading: false, seq: 0 },
    viewer: { items: [], index: 0, open: false, sheet: false, from: 'photo' }
  };

  const grids = {
    photo: new Masonry($('#grid-photo'), { minCol: 288, gap: 12, maxCols: 6 }),
    video: new Masonry($('#grid-video'), { minCol: 288, gap: 12, maxCols: 6 }),
    folder: new Masonry($('#grid-folder-files'), { minCol: 168, gap: 10, maxCols: 8 })
  };

  /* ---------------- 切换视图 ---------------- */
  const dockItems = $$('.dock-item[data-view]');

  /* ---------------- 主题 ---------------- */
  const THEME_KEY = 'fnphotowall.theme';
  function getTheme() {
    try {
      const t = localStorage.getItem(THEME_KEY);
      if (t === 'light' || t === 'dark') return t;
    } catch (e) {}
    return 'dark';
  }
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', t === 'light' ? '#f2f3f6' : '#0a0b0e');
    $$('#themeOptions button').forEach(b => b.classList.toggle('is-on', b.dataset.theme === t));
  }
  function setTheme(t) {
    try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
    applyTheme(t);
    relayoutAll();
  }
  function setView(view, opts) {
    state.view = view;
    dockItems.forEach(b => b.classList.toggle('is-active', b.dataset.view === view));
    $$('.view').forEach(v => v.classList.toggle('is-active', v.dataset.view === view));
    document.body.dataset.view = view;
    window.scrollTo({ top: 0, behavior: opts && opts.instant ? 'auto' : 'auto' });
    requestAnimationFrame(() => {
      if (view === 'photo' || view === 'video') grids[view].layout();
      else grids.folder.layout();
    });
    nextFrame(() => grids[view] && grids[view].layout());
    if (view === 'folder' && !state.folder.data && !(opts && opts.skipLoad)) loadFolder(state.folder.path, false);
    // 视频流按需加载：打开相册时只请求图片，切到视频再拉，缩短首屏等待
    if (view === 'video' && !state.feeds.video.loaded) loadFeed('video', true);
  }
  dockItems.forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.view === state.view) {
        if (state.view === 'folder') loadFolder(state.folder.path, false);
        return;
      }
      setView(btn.dataset.view);
    });
  });

  /* ---------------- 设置面板 ---------------- */
  const settingsEl = $('#settings');

  function renderCacheInfo(cache) {
    if (!cache) return;
    $('#cacheUsed').textContent = cache.bytesText + ' / ' + cache.limitText + ' · ' + cache.count + ' 张';
    $('#cacheDesc').textContent =
      '已缓存 ' + cache.count + ' 张缩略图，占用 ' + cache.bytesText +
      '。上限 ' + cache.limitText + '，超出后自动清理最久未使用的（至少保留 ' + cache.minKeep + ' 张）。';
    const gb = Math.round(cache.limitGb);
    let matched = false;
    $$('#cacheOptions button').forEach(b => {
      const on = Number(b.dataset.gb) === gb;
      matched = matched || on;
      b.classList.toggle('is-on', on);
    });
    // 非预设值（例如配置文件里写了 12GB）时不选中任何一项
    if (!matched) $$('#cacheOptions button').forEach(b => b.classList.remove('is-on'));
  }

  function renderCacheDirs(dirs) {
    const box = $('#cacheDirList');
    if (!box) return;
    box.innerHTML = '';
    const list = dirs || [];
    const cur = list.find(d => d.current);
    $('#cacheDirNow').textContent = cur ? cur.label : '—';
    list.forEach(d => {
      const b = document.createElement('button');
      b.className = 'path-item' + (d.current ? ' is-on' : '');
      b.dataset.path = d.path;
      b.disabled = !d.writable;
      b.innerHTML = '<span class="pi-radio"></span><span class="pi-main"><b></b><i></i></span>' +
                    '<span class="pi-meta"></span>';
      b.querySelector('b').textContent = d.label;
      b.querySelector('i').textContent = d.path;
      b.querySelector('.pi-meta').textContent = d.writable
        ? ('剩余 ' + (d.freeText || '—')) : '不可写';
      b.addEventListener('click', () => switchCacheDir(d));
      box.appendChild(b);
    });
  }

  async function switchCacheDir(d) {
    if (d.current) return;
    const box = $('#cacheDirList');
    $$('.path-item', box).forEach(x => { x.disabled = true; });
    try {
      const r = await post('/api/settings', { cache_dir: d.path });
      renderCacheDirs(r.cacheDirs);
      renderCacheInfo(r.cache);
      toast('缓存位置已切换，缩略图将重新生成');
    } catch (e) {
      toast('切换失败：' + e.message, true);
      refreshSettings();
    }
  }

  async function refreshSettings() {
    try {
      const d = await api('/api/settings');
      renderCacheInfo(d.cache);
      renderCacheDirs(d.cacheDirs);
    } catch (e) {
      $('#cacheUsed').textContent = '读取失败';
    }
  }

  /* ---------- 预加载缩略图 ---------- */
  let preTimer = 0;
  const fmtNum = n => (n || 0).toLocaleString('zh-CN');

  function renderPreload(st) {
    if (!st) return;
    const running = !!st.running;
    const finished = !running && st.reason === 'done';
    const atLimit = !running && st.reason === 'limit';
    $('#preStatus').textContent = running ? '进行中'
      : finished ? '已完成' : atLimit ? '已达上限'
      : st.reason === 'error' ? '出错' : '未开始';
    const pct = typeof st.percent === 'number' ? st.percent : 0;
    $('#preBar').style.width = Math.max(0, Math.min(100, pct)) + '%';
    const parts = [];
    if (st.total) parts.push('已处理 ' + fmtNum(st.processed) + ' / ' + fmtNum(st.total) + ' 张');
    parts.push('缓存池 ' + fmtNum(st.cached) + ' 张 · ' + st.bytesText + ' / ' + st.limitText);
    if (running && st.queue) parts.push('待生成 ' + fmtNum(st.queue));
    $('#preMeta').textContent = parts.join('  ·  ');
    $('#preStart').disabled = running || !!st.full;
    $('#preStop').disabled = !running;
    $('#preStart').textContent = (finished || atLimit) ? '重新预加载' : '开始预加载';
  }

  async function refreshPreload() {
    try {
      const d = await api('/api/preload');
      renderPreload(d.status);
    } catch (e) { /* 忽略 */ }
  }
  function startPreloadPolling() {
    stopPreloadPolling();
    refreshPreload();
    preTimer = setInterval(refreshPreload, 1500);
  }
  function stopPreloadPolling() {
    if (preTimer) { clearInterval(preTimer); preTimer = 0; }
  }

  $('#preStart').addEventListener('click', async () => {
    $('#preStart').disabled = true;
    try {
      const r = await post('/api/preload', { action: 'start' });
      renderPreload(r.status);
      toast('已开始预加载，缩略图会持续生成到缓存上限');
      startPreloadPolling();
    } catch (e) {
      toast('启动失败：' + e.message, true);
      $('#preStart').disabled = false;
    }
  });
  $('#preStop').addEventListener('click', async () => {
    try {
      const r = await post('/api/preload', { action: 'stop' });
      renderPreload(r.status);
      toast('已暂停预加载');
    } catch (e) {
      toast('暂停失败：' + e.message, true);
    }
  });

  function openSettings() {
    settingsEl.classList.add('is-open');
    settingsEl.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    applyTheme(getTheme());
    refreshSettings();
    startPreloadPolling();
  }
  function closeSettings() {
    settingsEl.classList.remove('is-open');
    settingsEl.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = state.viewer.open ? 'hidden' : '';
    stopPreloadPolling();
  }

  $('#btnSettings').addEventListener('click', openSettings);
  $('#setClose').addEventListener('click', closeSettings);
  settingsEl.addEventListener('click', e => { if (e.target === settingsEl) closeSettings(); });

  $$('#cacheOptions button').forEach(btn => {
    btn.addEventListener('click', async () => {
      const gb = Number(btn.dataset.gb);
      if (btn.disabled) return;
      btn.disabled = true;
      try {
        const r = await post('/api/settings', { cache_limit_gb: gb });
        renderCacheInfo(r.cache);
        if (r.cacheDirs) renderCacheDirs(r.cacheDirs);
        toast('缓存上限已设为 ' + gb + ' GB');
      } catch (e) {
        toast('设置失败：' + e.message, true);
      } finally {
        btn.disabled = false;
      }
    });
  });

  $('#cachePrune').addEventListener('click', async () => {
    const btn = $('#cachePrune');
    btn.disabled = true;
    btn.textContent = '清理中…';
    try {
      const r = await post('/api/cache', { prune: true });
      renderCacheInfo(r.stats);
      toast(r.prune && r.prune.removed ? '已清理 ' + r.prune.removed + ' 张缩略图' : '没有需要清理的缩略图');
    } catch (e) {
      toast('清理失败：' + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '立即清理无用缩略图';
    }
  });

  $$('#themeOptions button').forEach(btn => {
    btn.addEventListener('click', () => setTheme(btn.dataset.theme));
  });

  /* ---------------- 首屏统计 ---------------- */
  function updateSubtitle() {
    const p = state.feeds.photo.total;
    const v = state.feeds.video.total;
    const total = p + v;
    if (!total) {
      $('#subtitle').textContent = '相册是空的，去授权一个文件夹吧';
      return;
    }
    const bits = [];
    if (p) bits.push(p + ' 张照片');
    if (v) bits.push(v + ' 个视频');
    $('#subtitle').textContent = bits.join(' · ');
  }

  async function loadCounts(refresh) {
    try {
      const rf = refresh ? '&refresh=1' : '';
      const [p, v] = await Promise.all([
        api('/api/media?kind=image&seed=1&offset=0&limit=1' + rf),
        api('/api/media?kind=video&seed=1&offset=0&limit=1' + rf)
      ]);
      state.feeds.photo.total = p.total;
      state.feeds.video.total = v.total;
      updateSubtitle();
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------- 瀑布流数据 ---------------- */
  const PAGE = 60;         // 每页 60 张：更小的一页更快返回，首屏秒开，靠服务端预生成保证下滑不断档
  const CHUNK = 30;        // 分帧插入 DOM，避免一次性卡顿
  const LOOKAHEAD_PX = 1400; // 距离底部还有 1.4 屏时就开始加载下一页
  const EAGER = 12;        // 首屏前 12 张优先加载

  /**
   * 帧回调：优先 rAF，但用 setTimeout 兜底。
   * 飞牛桌面里应用跑在 iframe 中，窗口还没显示时 rAF 不会触发，
   * 只用 rAF 会导致首次进入空白、刷新后才正常。
   */
  function nextFrame(fn) {
    let done = false;
    const run = () => { if (done) return; done = true; fn(); };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
    setTimeout(run, 120);
  }

  function relayoutAll() {
    ['photo', 'video', 'folder'].forEach(k => grids[k].schedule());
  }

  /** 分批把卡片插入瀑布流，保持主线程可交互 */
  function appendCards(kind, items, startIndex, small) {
    return new Promise(resolve => {
      const grid = grids[kind];
      let i = 0;
      const step = () => {
        const frag = document.createDocumentFragment();
        const end = Math.min(i + CHUNK, items.length);
        for (; i < end; i++) {
          const item = items[i];
          const idx = startIndex + i;              // 必须按值捕获，否则所有卡片都会指向同一张
          const card = makeCard(item, small, i < EAGER && !small);
          card.grid = grid;
          card.item = item;
          card.index = idx;
          card.el.addEventListener('click', () => openViewer(kind, idx));
          grid.add(card);
          frag.appendChild(card.el);
        }
        grid.el.appendChild(frag);
        grid.layout();
        if (i < items.length) nextFrame(step);
        else resolve();
      };
      step();
    });
  }

  async function loadFeed(kind, reset, refresh) {
    const f = state.feeds[kind];
    const grid = grids[kind];
    if (f.loading) return;
    if (reset) {
      f.items = [];
      f.offset = 0;
      f.done = false;
      f.ready = false;
      f.seed = rand();
      grid.clear();
      $('#empty-' + kind).hidden = true;
    }
    if (f.done) return;

    f.loading = true;
    $('#sent-' + kind).classList.add('is-loading');
    try {
      const q = '/api/media?kind=' + f.kind + '&seed=' + f.seed + '&offset=' + f.offset +
                '&limit=' + PAGE + (refresh ? '&refresh=1' : '');
      const data = await api(q);
      f.total = data.total;
      f.loaded = true;
      const startIndex = f.items.length;
      // 先把数据放进列表（大图浏览依赖这个顺序），再分帧渲染
      data.items.forEach((item, i) => {
        item._idx = startIndex + i;
        f.items.push(item);
      });
      f.offset += data.items.length;
      if (f.offset >= f.total || !data.items.length) f.done = true;
      if (!f.total) $('#empty-' + kind).hidden = false;
      updateSubtitle();
      await appendCards(kind, data.items, startIndex, false);
      f.ready = true;
    } catch (e) {
      toast('加载失败：' + e.message, true);
    } finally {
      f.loading = false;
      $('#sent-' + kind).classList.remove('is-loading');
    }
  }

  function setupInfinite(kind) {
    const sentinel = $('#sent-' + kind);
    if (!('IntersectionObserver' in window)) return;
    const io = new IntersectionObserver(entries => {
      entries.forEach(en => {
        if (!en.isIntersecting || state.view !== kind) return;
        // 首屏还没排完版时哨兵贴在顶部，别急着加载第二页
        if (!state.feeds[kind].ready) return;
        const grid = grids[kind];
        if (!grid.el.clientWidth || !parseFloat(grid.el.style.height || '0')) return;
        const r = sentinel.getBoundingClientRect();
        // 还没接近底部就不预取，避免首屏渲染完就猛拉几十页
        if (r.top < window.innerHeight + LOOKAHEAD_PX) loadFeed(kind, false);
      });
    }, { rootMargin: '1400px 0px' });
    io.observe(sentinel);
  }

  /* ---------------- 文件夹视图 ---------------- */
  const FOLDER_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.1c.6 0 1.2.24 1.6.7l.9 1.05c.4.46 1 .75 1.6.75h5.8A2.5 2.5 0 0 1 21 10v6.5A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z"/></svg>';

  function renderPathbar(data) {
    const bar = $('#pathbar');
    bar.innerHTML = '';
    const drive = document.createElement('span');
    drive.className = 'drive';
    drive.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><circle cx="7" cy="7.5" r=".8" fill="currentColor"/></svg>';
    bar.appendChild(drive);

    const crumbs = data.crumbs || [];
    if (!crumbs.length) {
      const s = document.createElement('span');
      s.className = 'crumb is-last';
      s.textContent = '相册';
      bar.appendChild(s);
      return;
    }
    crumbs.forEach((c, i) => {
      if (i > 0) {
        const sep = document.createElement('span');
        sep.className = 'crumb-sep';
        sep.textContent = '›';
        bar.appendChild(sep);
      }
      const b = document.createElement('button');
      b.className = 'crumb' + (i === crumbs.length - 1 ? ' is-last' : '');
      b.textContent = c.name;
      b.addEventListener('click', () => {
        if (c.path !== state.folder.path) loadFolder(c.path, true);
      });
      bar.appendChild(b);
    });
    requestAnimationFrame(() => { bar.scrollLeft = bar.scrollWidth; });
  }

  async function loadFolder(path, push, refresh) {
    const st = state.folder;
    // 每次加载递增序号：并发/快速连点时，只有最新一次的结果会落地，旧的直接丢弃
    const token = ++st.seq;
    st.loading = true;
    st.path = path || '';                 // 先更新目标路径，失败/慢请求期间视图也不至于“不知道要去哪”
    try {
      const data = await withTimeout(
        api('/api/folder?path=' + encodeURIComponent(path || '') +
            (refresh ? '&refresh=1' : '')),
        8000, '打开文件夹超时');
      if (token !== st.seq) return;      // 已有更新的加载，放弃本次结果
      st.data = data;
      if (push) {
        st.stack = st.stack.slice(0, st.index + 1);
        if (st.stack[st.index] !== st.path) {
          st.stack.push(st.path);
          st.index = st.stack.length - 1;
        }
      }
      renderPathbar(data);
      renderFolders(data);
      renderFolderFiles(data);
      syncNavButtons();
      $('#copyPath').disabled = !st.path;
    } catch (e) {
      if (token === st.seq) toast('打开文件夹失败：' + e.message, true);
    } finally {
      if (token === st.seq) st.loading = false;
    }
  }

  function syncNavButtons() {
    const st = state.folder;
    $('#navBack').disabled = st.index <= 0;
    $('#navFwd').disabled = st.index >= st.stack.length - 1;
    $('#navUp').disabled = !st.path;
  }

  function renderFolders(data) {
    const grid = $('#folderGrid');
    grid.innerHTML = '';
    const folders = data.folders || [];
    $('#folderSection').hidden = !folders.length;

    folders.forEach((f, i) => {
      const tile = document.createElement('button');
      tile.className = 'folder-tile';
      const tone = f.favorites ? 'gold' : (i % 3 === 1 ? 'mint' : '');
      const cover = f.cover
        ? '<img src="' + BASE + f.cover + '" alt="" loading="lazy" onload="this.classList.add(\'loaded\')">'
        : '<div class="folder-empty-cover">' + FOLDER_SVG + '</div>';
      tile.innerHTML =
        '<div class="folder-cover">' +
          '<div class="folder-tab ' + tone + '"></div>' +
          cover +
        '</div>' +
        '<div class="folder-info">' +
          '<div class="folder-name">' + (f.favorites ? '<span class="star">★</span>' : '') +
            '<span class="fname"></span></div>' +
          '<div class="folder-sub">' + (f.count ? f.count + ' 项' : '空文件夹') + '</div>' +
        '</div>' +
        '<div class="folder-arrow"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></div>';
      tile.querySelector('.fname').textContent = f.name;
      tile.addEventListener('click', () => loadFolder(f.path, true));
      grid.appendChild(tile);
    });
  }

  function renderFolderFiles(data) {
    const grid = grids.folder;
    grid.clear();
    const files = data.files || [];
    $('#filesHead').hidden = !files.length;
    $('#filesCount').textContent = files.length ? files.length + ' 个文件' : '';
    $('#empty-folder').hidden = !!(data.folders && data.folders.length) || !!files.length;
    if (files.length) appendCards('folder', files, 0, true);
  }

  $('#navBack').addEventListener('click', () => {
    const st = state.folder;
    if (st.index > 0) { st.index--; loadFolder(st.stack[st.index], false); }
  });
  $('#navFwd').addEventListener('click', () => {
    const st = state.folder;
    if (st.index < st.stack.length - 1) { st.index++; loadFolder(st.stack[st.index], false); }
  });
  $('#navUp').addEventListener('click', () => {
    const st = state.folder;
    if (!st.path) return;
    const crumbs = (st.data && st.data.crumbs) || [];
    const parent = crumbs.length > 1 ? crumbs[crumbs.length - 2].path : '';
    loadFolder(parent, true);
  });
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) { /* 回退 */ }
    // HTTP 环境下没有 clipboard API，用隐藏 textarea + execCommand
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, text.length);
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (e) {
      return false;
    }
  }

  $('#copyPath').addEventListener('click', async () => {
    const p = state.folder.path || (state.folder.data && state.folder.data.path) || '';
    if (!p) {
      toast('当前是根目录，进入具体文件夹后再复制', true);
      return;
    }
    const ok = await copyText(p);
    toast(ok ? '已复制：' + p : '复制失败，可手动选中地址', !ok);
  });

  $('#folderRefresh').addEventListener('click', () => {
    fetch(BASE + '/api/folder?path=' + encodeURIComponent(state.folder.path || '') + '&refresh=1').catch(() => {});
    loadFolder(state.folder.path, false);
  });

  /* ---------------- 大图浏览 ---------------- */
  const viewer = $('#viewer');
  const vImage = $('#vImage');
  const vVideo = $('#vVideo');
  const vStage = $('#vStage');
  const sheet = $('#sheet');
  let swapToken = 0;

  // 视频编码不受支持时给个明确提示，而不是一直转圈
  vVideo.addEventListener('error', () => {
    if (!state.viewer.open) return;
    const it = state.viewer.items[state.viewer.index];
    if (!it || it.kind !== 'video') return;
    toast('该视频无法播放：浏览器可能不支持它的编码格式', true);
  });

  const PREVIEW_ZOOM = 8;      // 预览图最大放大倍率
  const FULL_ZOOM = 16;        // 原图最大放大倍率
  let fullState = { path: null, loading: false, loaded: false, failed: false };

  function previewWidth() {
    const w = window.innerWidth || 1440;
    if (w <= 720) return 1280;
    if (w <= 1900) return 1920;
    return 2560;
  }

  /** 放大上限：至少 6×，最多 16×，但不会超过图像自身像素密度的 3 倍（再放大只会更糊） */
  function zoomMaxFor() {
    const nat = nativeRatio();
    if (!isFinite(nat) || nat <= 0) return PREVIEW_ZOOM;
    return clamp(nat * 3, 6, FULL_ZOOM);
  }

  /** 当前这张图「1 图像像素 = 1 CSS 像素」所需的缩放倍率 */
  function nativeRatio() {
    const nat = vImage.naturalWidth;
    const disp = vImage.offsetWidth || 0;
    if (!nat || !disp) return 1;
    return nat / disp;
  }

  function isFullLoaded(item) {
    return !!item && fullState.path === item.path && fullState.loaded;
  }

  /** 只保留「上级文件夹 / 所在文件夹」两级，用于大图左上角 */
  function folderLabels(p) {
    const parts = String(p || '').split(/[\\/]+/).filter(Boolean);
    if (parts.length <= 1) return [];
    parts.pop();                                   // 去掉文件名
    return parts.slice(-3);                        // 只保留最近三级
  }

  /** 去掉文件名，得到所在文件夹的绝对路径 */
  function dirOf(p) {
    const s = String(p || '');
    const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
    return i > 0 ? s.slice(0, i) : s;
  }

  const FOLDER_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.1c.6 0 1.2.24 ' +
    '1.6.7l.9 1.05c.4.46 1 .75 1.6.75h5.8A2.5 2.5 0 0 1 21 10v6.5A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 ' +
    '3 16.5z"/></svg>';

  function updateMeta(item) {
    if (!item) return;
    const v = state.viewer;
    const pv = $('#vPath');
    if (pv) {
      const labels = folderLabels(item.path);
      if (labels.length) {
        pv.hidden = false;
        pv.innerHTML = FOLDER_ICON + '<span></span>';
        pv.querySelector('span').textContent = labels.join('  /  ');
        pv.title = labels.join(' / ');
      } else {
        pv.hidden = true;
      }
    }
    const bits = [];
    if (item.kind === 'video') bits.push('视频');
    if (item.kind === 'image') bits.push(isFullLoaded(item) ? '原图' : '预览');
    if (item.w && item.h) bits.push(item.w + ' × ' + item.h);
    if (item.size) bits.push(fmtSize(item.size));
    bits.push((v.index + 1) + ' / ' + v.items.length);
    $('#vMeta').textContent = bits.join('  ·  ');
  }

  function updateOrigButton(item) {
    const btn = $('#vUltra');
    if (!btn) return;
    if (!item || item.kind !== 'image') { btn.hidden = true; return; }
    btn.hidden = false;
    const same = fullState.path === item.path;
    const loaded = same && fullState.loaded;
    const loading = same && fullState.loading;
    btn.classList.toggle('is-on', loaded);
    btn.classList.toggle('is-busy', loading);
    btn.querySelector('span').textContent = loaded ? '超清 ✓' : (loading ? '加载中' : '超清');
  }

  function updateOneButton() {
    const btn = $('#vOne');
    if (!btn) return;
    const item = state.viewer.items[state.viewer.index];
    const show = state.viewer.open && item && item.kind === 'image';
    btn.hidden = !show;
    if (!show) return;
    const nat = nativeRatio();
    btn.classList.toggle('is-on', zoom.s >= nat * 0.96 && nat > 1.01);
  }

  /**
   * 加载原图。
   * silent=true 时不弹提示、不自动放大（用于「放大到超过预览分辨率」时的自动升级）
   */
  function loadOriginal(silent) {
    const item = state.viewer.items[state.viewer.index];
    if (!item || item.kind !== 'image') return Promise.resolve(false);
    if (fullState.path === item.path && (fullState.loading || fullState.loaded)) {
      return Promise.resolve(fullState.loaded);
    }
    if (fullState.path === item.path && fullState.failed && !silent) {
      // 手动重试
    }

    const wasLowZoom = zoom.s <= 1.05;
    fullState = { path: item.path, loading: true, loaded: false, failed: false };
    updateOrigButton(item);
    const token = swapToken;

    return new Promise(resolve => {
      const img = new Image();
      img.decoding = 'async';
      img.onload = () => {
        if (token !== swapToken) { resolve(false); return; }
        fullState.loading = false;
        fullState.loaded = true;
        vImage.src = img.src;
        vImage.classList.add('is-shown');
        updateOrigButton(item);
        updateMeta(item);
        updateOneButton();
        if (!silent) {
          toast('已载入原图 ' + img.naturalWidth + '×' + img.naturalHeight + '，可放大到 16×');
          // 用户是主动想看细节的：把画面推近一些，让清晰度变化立刻可见
          if (wasLowZoom) {
            setTimeout(() => {
              if (token !== swapToken) return;
              zoom.s = 2;
              zoom.x = 0;
              zoom.y = 0;
              clampZoomPan();
              applyZoom(true);
            }, 80);
          }
        }
        resolve(true);
      };
      img.onerror = () => {
        if (token !== swapToken) { resolve(false); return; }
        fullState.loading = false;
        fullState.failed = true;
        updateOrigButton(item);
        if (!silent) toast('原图加载失败，可重试或检查文件权限', true);
        resolve(false);
      };
      img.src = fileUrl(item.path) + '&v=' + (item.mtime || 0);
    });
  }

  /** 放大到超过预览像素时，自动换成原图——这样继续放大不会再糊 */
  function maybeAutoUpgrade() {
    if (!state.viewer.open) return;
    const item = state.viewer.items[state.viewer.index];
    if (!item || item.kind !== 'image') return;
    if (fullState.path === item.path && (fullState.loaded || fullState.loading)) return;
    if (fullState.path === item.path && fullState.failed) return;
    const nat = nativeRatio();
    if (zoom.s > nat * 0.97) loadOriginal(true);
  }

  /** 1:1：按图像原始像素显示 */
  async function zoomToNative() {
    const item = state.viewer.items[state.viewer.index];
    if (!item || item.kind !== 'image') return;
    if (!isFullLoaded(item)) {
      const ok = await loadOriginal(true);
      if (!ok) { toast('原图加载失败，无法 1:1 显示', true); return; }
    }
    const nat = nativeRatio();
    zoom.s = clamp(nat, 1, FULL_ZOOM);
    zoom.x = 0;
    zoom.y = 0;
    clampZoomPan();
    applyZoom(true);
    updateOneButton();
  }

  function currentItems(from) {
    if (from === 'folder') {
      const d = state.folder.data;
      return (d && d.files) || [];
    }
    return state.feeds[from].items;
  }

  function openViewer(from, index) {
    state.viewer.from = from;
    state.viewer.items = currentItems(from).slice();
    state.viewer.index = index;
    state.viewer.open = true;
    viewer.classList.add('is-open');
    viewer.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    closeSheet(true);
    renderViewer();
  }

  function closeViewer() {
    state.viewer.open = false;
    if (typeof closeUltra === 'function') closeUltra();
    viewer.classList.remove('is-open');
    viewer.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    closeSheet(true);
    stopVideo();
    resetZoom(false);
  }

  /* ---------------- 大图缩放（滚轮 / 捏合 / 双击 / 拖动） ---------------- */
  const zoom = { s: 1, x: 0, y: 0 };

  function canZoom() {
    if (!state.viewer.open) return false;
    const it = state.viewer.items[state.viewer.index];
    return !!it && it.kind === 'image';
  }
  function applyZoom(animate) {
    vImage.style.transition = animate ? 'transform .22s cubic-bezier(.22,.68,.28,1)' : 'none';
    vImage.style.transform = zoom.s === 1
      ? ''
      : 'translate3d(' + zoom.x + 'px,' + zoom.y + 'px,0) scale(' + zoom.s + ')';
    vImage.classList.toggle('is-zoomed', zoom.s > 1.01);
    updateOneButton();
    // 放大到超过预览图自身的像素密度时，自动换成原图，继续放大才不会糊
    if (zoom.s > 1.01) maybeAutoUpgrade();
  }
  function resetZoom(animate) {
    zoom.s = 1; zoom.x = 0; zoom.y = 0;
    applyZoom(animate);
  }
  function clampZoomPan() {
    const maxX = Math.max(0, (zoom.s - 1) * vImage.offsetWidth / 2);
    const maxY = Math.max(0, (zoom.s - 1) * vImage.offsetHeight / 2);
    zoom.x = clamp(zoom.x, -maxX, maxX);
    zoom.y = clamp(zoom.y, -maxY, maxY);
  }
  function zoomAt(px, py, factor, animate) {
    const next = clamp(zoom.s * factor, 1, zoomMaxFor());
    if (Math.abs(next - zoom.s) < 0.0005) return;
    if (next <= 1.001) {
      zoom.s = 1; zoom.x = 0; zoom.y = 0;
      applyZoom(animate);
      return;
    }
    // 以图片当前视觉中心反推未变换中心，保证光标下的像素不动
    const r = vImage.getBoundingClientRect();
    const bcx = r.left + r.width / 2 - zoom.x;
    const bcy = r.top + r.height / 2 - zoom.y;
    const dx = (px - bcx - zoom.x) / zoom.s;
    const dy = (py - bcy - zoom.y) / zoom.s;
    zoom.s = next;
    zoom.x = px - bcx - dx * zoom.s;
    zoom.y = py - bcy - dy * zoom.s;
    clampZoomPan();
    applyZoom(animate);
  }
  function zoomToggle(px, py) {
    if (zoom.s > 1.05) {
      zoom.s = 1; zoom.x = 0; zoom.y = 0;
      applyZoom(true);
    } else {
      zoomAt(px, py, 2.6, true);
    }
  }

  // 滚轮 / 触控板捏合
  vStage.addEventListener('wheel', e => {
    if (!canZoom()) return;
    e.preventDefault();
    const factor = Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.0018));
    zoomAt(e.clientX, e.clientY, factor, false);
  }, { passive: false });

  // 双击放大 / 复原
  vImage.addEventListener('dblclick', e => {
    if (!canZoom()) return;
    e.preventDefault();
    zoomToggle(e.clientX, e.clientY);
  });

  // 鼠标拖动平移（仅放大后）
  let drag = null;
  vImage.addEventListener('mousedown', e => {
    if (!canZoom() || zoom.s <= 1.01) return;
    drag = { x: e.clientX, y: e.clientY, ox: zoom.x, oy: zoom.y };
    vImage.classList.add('is-dragging');
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (!drag) return;
    zoom.x = drag.ox + (e.clientX - drag.x);
    zoom.y = drag.oy + (e.clientY - drag.y);
    clampZoomPan();
    applyZoom(false);
  });
  window.addEventListener('mouseup', () => {
    if (!drag) return;
    drag = null;
    vImage.classList.remove('is-dragging');
  });

  // 触屏：双指捏合缩放、单指拖动平移
  let pinch = null;
  let panTouch = null;
  const dist2 = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);

  vStage.addEventListener('touchstart', e => {
    if (!canZoom()) return;
    if (e.touches.length === 2) {
      const a = e.touches[0], b = e.touches[1];
      pinch = { d: dist2(a, b) };
      panTouch = null;
    } else if (e.touches.length === 1 && zoom.s > 1.01) {
      panTouch = { x: e.touches[0].clientX, y: e.touches[0].clientY, ox: zoom.x, oy: zoom.y };
    }
  }, { passive: true });

  vStage.addEventListener('touchmove', e => {
    if (!canZoom()) return;
    if (pinch && e.touches.length === 2) {
      const a = e.touches[0], b = e.touches[1];
      const d = dist2(a, b);
      if (pinch.d > 0) zoomAt((a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2, d / pinch.d, false);
      pinch.d = d;
      e.preventDefault();
    } else if (panTouch && e.touches.length === 1) {
      const t = e.touches[0];
      zoom.x = panTouch.ox + (t.clientX - panTouch.x);
      zoom.y = panTouch.oy + (t.clientY - panTouch.y);
      clampZoomPan();
      applyZoom(false);
      e.preventDefault();
    }
  }, { passive: false });

  vStage.addEventListener('touchend', e => {
    if (e.touches.length < 2) pinch = null;
    if (e.touches.length === 0) panTouch = null;
    if (zoom.s > 1 && zoom.s < 1.05) { zoom.s = 1; zoom.x = 0; zoom.y = 0; applyZoom(true); }
  }, { passive: true });

  // 双击 / 双击（触屏）切换缩放：移动端 WebView 不一定派发 dblclick
  let lastTap = 0, lastTapX = 0, lastTapY = 0;
  vStage.addEventListener('touchend', e => {
    if (!e.changedTouches.length || !canZoom()) return;
    const t = e.changedTouches[0];
    const now = Date.now();
    const dx = Math.abs(t.clientX - lastTapX);
    const dy = Math.abs(t.clientY - lastTapY);
    if (now - lastTap < 320 && dx < 40 && dy < 40) {
      lastTap = 0;
      zoomToggle(t.clientX, t.clientY);
    } else {
      lastTap = now;
      lastTapX = t.clientX;
      lastTapY = t.clientY;
    }
  }, { passive: true });

  function stopVideo() {
    try { vVideo.pause(); } catch (e) {}
    vVideo.removeAttribute('src');
    vVideo.load();
  }

  function renderViewer() {
    const v = state.viewer;
    const items = v.items;
    if (!items.length) { closeViewer(); return; }
    v.index = clamp(v.index, 0, items.length - 1);
    const item = items[v.index];

    $('#vName').textContent = item.name || '';

    $('#vPrev').disabled = v.index <= 0;
    $('#vNext').disabled = v.index >= items.length - 1;

    updateFavButton(!!item.fav);
    resetZoom(false);

    const token = ++swapToken;
    fullState = { path: null, loading: false, loaded: false, failed: false };
    updateMeta(item);          // 必须在重置 fullState 之后，否则会显示上一张的「原图」状态
    if (item.kind === 'video') {
      vImage.classList.remove('is-shown');
      vImage.removeAttribute('src');
      vVideo.classList.add('is-shown');
      vVideo.src = fileUrl(item.path);
      vVideo.poster = thumbUrl(item.path, 960, item.mtime);
      vVideo.load();
    } else {
      stopVideo();
      vVideo.classList.remove('is-shown');
      // 先上高清预览（1920/2560），秒开；放大超过预览分辨率时自动换原图，也可手动点「原图」
      const preview = thumbUrl(item.path, previewWidth(), item.mtime);
      vImage.classList.remove('is-shown');
      vImage.src = preview;
      vImage.onload = () => {
        if (token !== swapToken) return;
        vImage.classList.add('is-shown');
        updateOneButton();
      };
      // 原图本身就不比预览大（小图/截图）时，直接视作已加载，避免白跑一次请求
      if (item.w && item.w <= previewWidth() + 8) {
        fullState = { path: item.path, loading: false, loaded: true, failed: false };
        updateOrigButton(item);
        updateMeta(item);
      }
    }
    updateOrigButton(item);
    updateOneButton();

    // 背景虚化
    $('#viewerBg').style.backgroundImage = item.kind === 'image'
      ? 'url("' + thumbUrl(item.path, 480, item.mtime) + '")'
      : 'url("' + thumbUrl(item.path, 480, item.mtime) + '")';

    if (v.sheet) renderSheet(item);
    preloadNeighbors();
  }

  function preloadNeighbors() {
    const v = state.viewer;
    [v.index + 1, v.index - 1].forEach(i => {
      const it = v.items[i];
      if (!it) return;
      const im = new Image();
      im.src = thumbUrl(it.path, 480, it.mtime);
    });
  }

  function updateFavButton(fav) {
    const btn = $('#vFav');
    btn.classList.toggle('is-on', fav);
    btn.querySelector('span').textContent = fav ? '已收藏' : '收藏';
  }

  function go(delta) {
    if (ultraOpen) return;   // 超清层打开时不切图
    const v = state.viewer;
    const next = v.index + delta;
    if (next < 0 || next >= v.items.length) return;
    v.index = next;
    if (state.viewer.sheet) { /* 保持详情打开，同步内容 */ }
    renderViewer();
  }

  $('#vPrev').addEventListener('click', () => go(-1));
  $('#vNext').addEventListener('click', () => go(1));
  $('#vClose').addEventListener('click', closeViewer);
  vStage.addEventListener('click', e => {
    if (e.target === vStage) closeViewer();
  });

  /* 键盘 */
  document.addEventListener('keydown', e => {
    if (settingsEl.classList.contains('is-open')) {
      if (e.key === 'Escape') closeSettings();
      return;
    }
    if (!state.viewer.open) return;
    // 超清层打开时，键盘操作只服务这一层：Esc 先退层，方向键/0 交给它自己
    if (ultraOpen) {
      if (e.key === 'Escape') { closeUltra(); e.preventDefault(); }
      else if (e.key === '0') { ultraReset(); }
      return;
    }
    if (e.key === 'Escape') { if (state.viewer.sheet) closeSheet(); else closeViewer(); }
    else if (e.key === 'ArrowLeft') go(-1);
    else if (e.key === 'ArrowRight') go(1);
    else if (e.key === 'i' || e.key === 'I') toggleSheet();
    else if (e.key === 'o' || e.key === 'O') openUltra();
    else if (e.key === '1') zoomToNative();
    else if (e.key === 'u' || e.key === 'U') loadOriginal(false);
    else if (e.key === '0') resetZoom(true);
  });

  /* 触摸滑动（放大或双指时交给缩放/拖动处理） */
  let touchX = 0, touchY = 0, touchT = 0;
  vStage.addEventListener('touchstart', e => {
    if (e.touches.length !== 1) return;
    touchX = e.touches[0].clientX;
    touchY = e.touches[0].clientY;
    touchT = Date.now();
  }, { passive: true });
  vStage.addEventListener('touchend', e => {
    if (!e.changedTouches.length) return;
    if (zoom.s > 1.01 || pinch) return;      // 放大状态不切图
    const dx = e.changedTouches[0].clientX - touchX;
    const dy = e.changedTouches[0].clientY - touchY;
    const dt = Date.now() - touchT;
    if (Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.6 && dt < 700) {
      go(dx < 0 ? 1 : -1);
    }
  }, { passive: true });

  /* ---------------- 收藏 ---------------- */
  async function toggleFavorite() {
    const item = state.viewer.items[state.viewer.index];
    if (!item) return;
    const btn = $('#vFav');
    if (btn.classList.contains('is-busy')) return;
    btn.classList.add('is-busy');
    try {
      const action = item.fav ? 'remove' : 'add';
      await post('/api/favorite', { path: item.path, action });
      item.fav = !item.fav;
      // 同步所有副本
      Object.keys(state.feeds).forEach(k => {
        state.feeds[k].items.forEach(it => { if (it.path === item.path) it.fav = item.fav; });
      });
      if (state.folder.data) {
        (state.folder.data.files || []).forEach(it => { if (it.path === item.path) it.fav = item.fav; });
      }
      updateFavButton(item.fav);
      refreshCardHeart(item.path, item.fav);
      toast(item.fav ? '已加入「我的收藏」文件夹' : '已取消收藏');
    } catch (e) {
      toast('操作失败：' + e.message, true);
    } finally {
      btn.classList.remove('is-busy');
    }
  }
  $('#vFav').addEventListener('click', toggleFavorite);
  // 功能互换后：「超清」= 应用内加载原图（可放大看像素）；「原图」= 新窗口按原始分辨率打开
  $('#vUltra').addEventListener('click', () => loadOriginal(false));
  $('#vOne').addEventListener('click', zoomToNative);

  /* ---------------- 超清层（应用内，绝不发生页面跳转） ---------------- */
  // 早先用独立 ultra.html 页面 + window.open，在飞牛自带 WebView 里返回会跳回主页重载。
  // 改为在同页面叠一个全屏层：点「原图」只是显示它，返回只是隐藏它，物理上不存在导航。
  const ultraLayer = $('#ultraLayer');
  const ultraStage = $('#ultraStage');
  const ultraImg = $('#ultraImg');
  const ultraZoom = { s: 1, x: 0, y: 0 };
  const ULTRA_MAX = 16;

  function ultraApply() {
    ultraImg.style.transform = ultraZoom.s === 1
      ? ''
      : 'translate3d(' + ultraZoom.x + 'px,' + ultraZoom.y + 'px,0) scale(' + ultraZoom.s + ')';
    ultraStage.style.cursor = ultraZoom.s > 1.01 ? 'grab' : 'zoom-in';
  }
  function ultraClamp() {
    const maxX = Math.max(0, (ultraZoom.s - 1) * ultraImg.offsetWidth / 2);
    const maxY = Math.max(0, (ultraZoom.s - 1) * ultraImg.offsetHeight / 2);
    ultraZoom.x = Math.min(maxX, Math.max(-maxX, ultraZoom.x));
    ultraZoom.y = Math.min(maxY, Math.max(-maxY, ultraZoom.y));
  }
  function ultraZoomAt(px, py, factor) {
    const next = Math.min(ULTRA_MAX, Math.max(1, ultraZoom.s * factor));
    if (Math.abs(next - ultraZoom.s) < 0.0005) return;
    if (next <= 1.001) { ultraZoom.s = 1; ultraZoom.x = 0; ultraZoom.y = 0; ultraApply(); return; }
    const r = ultraImg.getBoundingClientRect();
    const bcx = r.left + r.width / 2 - ultraZoom.x;
    const bcy = r.top + r.height / 2 - ultraZoom.y;
    const dx = (px - bcx - ultraZoom.x) / ultraZoom.s;
    const dy = (py - bcy - ultraZoom.y) / ultraZoom.s;
    ultraZoom.s = next;
    ultraZoom.x = px - bcx - dx * ultraZoom.s;
    ultraZoom.y = py - bcy - dy * ultraZoom.s;
    ultraClamp();
    ultraApply();
  }
  function ultraReset() {
    ultraZoom.s = 1; ultraZoom.x = 0; ultraZoom.y = 0;
    ultraApply();
  }

  function openUltra() {
    const item = state.viewer.items[state.viewer.index];
    if (!item) return;
    ultraOpen = true;
    ultraLayer.hidden = false;
    ultraLayer.setAttribute('aria-hidden', 'false');
    ultraReset();
    $('#ultraName').textContent = item.name || '';
    $('#ultraMeta').textContent = '正在加载原图…';
    const hint = $('#ultraHint');
    hint.classList.remove('gone');
    clearTimeout(hint._t);
    hint._t = setTimeout(() => hint.classList.add('gone'), 3200);
    ultraImg.onload = () => {
      $('#ultraMeta').textContent = ultraImg.naturalWidth + ' × ' + ultraImg.naturalHeight +
        '  ·  原图  ·  ' + Math.round(ultraImg.naturalWidth / (ultraImg.offsetWidth || 1) * 10) / 10 + '× 可放大';
      ultraReset();
    };
    ultraImg.onerror = () => { $('#ultraMeta').textContent = '原图加载失败，可能文件已被移动或没有读取权限'; };
    ultraImg.src = BASE + '/api/file?path=' + encodeURIComponent(item.path) + '&inline=1';
    // 触发过渡（先 display 再切 class）
    requestAnimationFrame(() => ultraLayer.classList.add('is-open'));
  }

  function closeUltra() {
    if (!ultraOpen) return;
    ultraOpen = false;
    ultraLayer.classList.remove('is-open');
    ultraLayer.setAttribute('aria-hidden', 'true');
    setTimeout(() => {
      if (ultraOpen) return;
      ultraLayer.hidden = true;
      ultraImg.removeAttribute('src');   // 释放大图内存
      ultraReset();
    }, 240);
  }

  $('#ultraBack').addEventListener('click', closeUltra);

  // 下载原图：用 download=1 让服务端返回 Content-Disposition: attachment
  $('#ultraDl').addEventListener('click', async () => {
    const item = state.viewer.items[state.viewer.index];
    if (!item) return;
    const btn = $('#ultraDl');
    btn.classList.add('is-busy');
    const url = BASE + '/api/file?path=' + encodeURIComponent(item.path) + '&download=1';
    try {
      const a = document.createElement('a');
      a.href = url;
      a.download = item.name || '';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      toast('开始下载：' + (item.name || '原图'));
    } catch (e) {
      toast('下载失败：' + e.message, true);
    } finally {
      setTimeout(() => btn.classList.remove('is-busy'), 1200);
    }
  });

  ultraStage.addEventListener('wheel', e => {
    if (!ultraOpen) return;
    e.preventDefault();
    ultraZoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.0018)));
  }, { passive: false });

  ultraStage.addEventListener('dblclick', e => {
    if (!ultraOpen) return;
    e.preventDefault();
    if (ultraZoom.s > 1.05) { ultraZoom.s = 1; ultraZoom.x = 0; ultraZoom.y = 0; ultraApply(); }
    else ultraZoomAt(e.clientX, e.clientY, 2.6);
  });

  // 桌面端拖动：Pointer Events + setPointerCapture（鼠标移出元素也能持续追踪）
  let ultraDrag = null, ultraDragId = null;
  ultraStage.addEventListener('pointerdown', e => {
    if (!ultraOpen || e.pointerType !== 'mouse' || e.button !== 0) return;
    if (ultraZoom.s <= 1.01) return;
    ultraDrag = { x: e.clientX, y: e.clientY, ox: ultraZoom.x, oy: ultraZoom.y };
    ultraDragId = e.pointerId;
    ultraStage.classList.add('dragging');
    e.preventDefault();
    try { ultraStage.setPointerCapture(e.pointerId); } catch (err) {}
  });
  ultraStage.addEventListener('pointermove', e => {
    if (!ultraDrag || ultraDragId !== e.pointerId) return;
    ultraZoom.x = ultraDrag.ox + (e.clientX - ultraDrag.x);
    ultraZoom.y = ultraDrag.oy + (e.clientY - ultraDrag.y);
    ultraClamp(); ultraApply();
  });
  const ultraEndDrag = e => {
    if (!ultraDrag || (e && ultraDragId !== e.pointerId)) return;
    ultraDrag = null; ultraDragId = null;
    ultraStage.classList.remove('dragging');
  };
  ultraStage.addEventListener('pointerup', ultraEndDrag);
  ultraStage.addEventListener('pointercancel', ultraEndDrag);

  // 触屏：双指捏合缩放、单指拖动平移
  let ultraPinch = null, ultraPanT = null;
  const uDist = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  ultraStage.addEventListener('touchstart', e => {
    if (!ultraOpen) return;
    if (e.touches.length === 2) {
      ultraPinch = { d: uDist(e.touches[0], e.touches[1]) };
      ultraPanT = null;
    } else if (e.touches.length === 1 && ultraZoom.s > 1.01) {
      ultraPanT = { x: e.touches[0].clientX, y: e.touches[0].clientY, ox: ultraZoom.x, oy: ultraZoom.y };
    }
  }, { passive: true });
  ultraStage.addEventListener('touchmove', e => {
    if (!ultraOpen) return;
    if (ultraPinch && e.touches.length === 2) {
      const a = e.touches[0], b = e.touches[1];
      const d = uDist(a, b);
      if (ultraPinch.d > 0) ultraZoomAt((a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2, d / ultraPinch.d);
      ultraPinch.d = d;
      e.preventDefault();
    } else if (ultraPanT && e.touches.length === 1) {
      const t = e.touches[0];
      ultraZoom.x = ultraPanT.ox + (t.clientX - ultraPanT.x);
      ultraZoom.y = ultraPanT.oy + (t.clientY - ultraPanT.y);
      ultraClamp(); ultraApply();
      e.preventDefault();
    }
  }, { passive: false });
  ultraStage.addEventListener('touchend', e => {
    if (e.touches.length < 2) ultraPinch = null;
    if (e.touches.length === 0) ultraPanT = null;
  }, { passive: true });

  $('#vOrig').addEventListener('click', openUltra);

  // 左上角的文件夹路径：点一下跳到文件夹视图（与详情页的跳转一致）
  $('#vPath').addEventListener('click', () => {
    const item = state.viewer.items[state.viewer.index];
    if (!item) return;
    jumpToFolder(dirOf(item.path));
  });

  /* ---------------- 删除 ---------------- */
  async function deleteCurrent() {
    const item = state.viewer.items[state.viewer.index];
    if (!item) return;
    const ok = await confirmBox(
      '删除这个文件？',
      '<b>' + escapeHtml(item.name) + '</b><br>文件将从磁盘上永久删除，回收站里也找不回来。',
      '永久删除'
    );
    if (!ok) return;
    const btn = $('#vDel');
    btn.classList.add('is-busy');
    try {
      await post('/api/delete', { path: item.path, confirm: true });
      removeItemEverywhere(item.path);
      toast('已删除');

      const v = state.viewer;
      if (!v.items.length) { closeViewer(); }
      else {
        if (v.index >= v.items.length) v.index = v.items.length - 1;
        renderViewer();
      }
      if (state.view === 'folder') loadFolder(state.folder.path, false);
    } catch (e) {
      toast('删除失败：' + e.message, true);
    } finally {
      btn.classList.remove('is-busy');
    }
  }
  $('#vDel').addEventListener('click', deleteCurrent);

  function removeItemEverywhere(path) {
    // 视图数据
    ['photo', 'video'].forEach(k => {
      const f = state.feeds[k];
      const before = f.items.length;
      f.items = f.items.filter(it => it.path !== path);
      if (f.items.length !== before) {
        f.total = Math.max(0, f.total - 1);
        f.offset = Math.max(0, f.offset - 1);
      }
      const grid = grids[k];
      const keep = grid.cards.filter(c => c.item.path !== path);
      if (keep.length !== grid.cards.length) {
        grid.cards.forEach(c => { if (c.item.path === path) c.el.remove(); });
        grid.cards = keep;
        grid.schedule();
      }
    });
    if (state.folder.data) {
      const files = state.folder.data.files || [];
      const before = files.length;
      state.folder.data.files = files.filter(it => it.path !== path);
      if (state.folder.data.files.length !== before) {
        const grid = grids.folder;
        grid.cards.forEach(c => { if (c.item.path === path) c.el.remove(); });
        grid.cards = grid.cards.filter(c => c.item.path !== path);
        grid.schedule();
        $('#filesCount').textContent = state.folder.data.files.length
          ? state.folder.data.files.length + ' 个文件' : '';
      }
    }
    // viewer 列表
    state.viewer.items = state.viewer.items.filter(it => it.path !== path);
    updateSubtitle();
  }

  /* ---------------- 详情 ---------------- */
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  let sheetToken = 0;
  async function renderSheet(item) {
    const body = $('#sheetBody');
    const token = ++sheetToken;
    body.innerHTML = '<div class="detail-empty">读取中…</div>';
    let d;
    try {
      d = await api('/api/detail?path=' + encodeURIComponent(item.path));
    } catch (e) {
      body.innerHTML = '<div class="detail-empty">读取详情失败：' + escapeHtml(e.message) + '</div>';
      return;
    }
    if (token !== sheetToken) return;

    const ex = d.exif || {};
    let html = '<div class="detail-group"><div class="detail-group-title">基本</div>';
    html += '<div class="detail-row"><div class="detail-key">文件名</div><div class="detail-val">' + escapeHtml(d.name) + '</div></div>';
    html += '<div class="detail-row"><div class="detail-key">类型</div><div class="detail-val">' +
      (d.kind === 'video' ? '视频' : d.kind === 'image' ? '图片' : '文件') +
      (d.ext ? ' · ' + d.ext.toUpperCase() : '') + '</div></div>';
    html += '<div class="detail-row"><div class="detail-key">尺寸</div><div class="detail-val">' +
      (d.w && d.h ? d.w + ' × ' + d.h : '—') + '</div></div>';
    html += '<div class="detail-row"><div class="detail-key">占用空间</div><div class="detail-val">' + d.sizeText + '</div></div>';
    html += '</div>';

    html += '<div class="detail-group"><div class="detail-group-title">时间</div>';
    if (ex.shot_at) html += '<div class="detail-row"><div class="detail-key">拍摄时间</div><div class="detail-val">' + escapeHtml(ex.shot_at) + '</div></div>';
    html += '<div class="detail-row"><div class="detail-key">修改时间</div><div class="detail-val">' + escapeHtml(d.mtimeText) + '</div></div>';
    html += '</div>';

    const hasExif = ex.camera || ex.lens || ex.aperture || ex.shutter || ex.iso || ex.focal;
    html += '<div class="detail-group"><div class="detail-group-title">拍摄参数</div>';
    if (hasExif) {
      if (ex.camera) html += '<div class="detail-row"><div class="detail-key">相机</div><div class="detail-val">' + escapeHtml(ex.camera) + '</div></div>';
      if (ex.lens) html += '<div class="detail-row"><div class="detail-key">镜头</div><div class="detail-val">' + escapeHtml(ex.lens) + '</div></div>';
      const cells = [];
      if (ex.aperture) cells.push(['光圈', ex.aperture]);
      if (ex.shutter) cells.push(['快门', ex.shutter]);
      if (ex.iso) cells.push(['ISO', ex.iso.replace(/^ISO\s*/i, '')]);
      if (ex.focal) cells.push(['焦距', ex.focal]);
      if (cells.length) {
        html += '<div class="exif-grid">' + cells.map(c =>
          '<div class="exif-cell"><b>' + escapeHtml(c[1]) + '</b><span>' + c[0] + '</span></div>').join('') + '</div>';
      }
    } else {
      html += '<div class="detail-empty">该文件没有可读取的拍摄参数</div>';
    }
    html += '</div>';

    html += '<div class="detail-group"><div class="detail-group-title">位置</div>';
    html += '<button class="path-box" id="pathLink" title="点击跳转到所在文件夹">' +
      '<svg class="path-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.1c.6 0 1.2.24 1.6.7l.9 1.05c.4.46 1 .75 1.6.75h5.8A2.5 2.5 0 0 1 21 10v6.5A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z"/></svg>' +
      '<span class="path-text">' + escapeHtml(d.dir) + '</span>' +
      '<span class="path-go">打开 ›</span></button>';
    html += '</div>';

    body.innerHTML = html;
    const link = $('#pathLink', body);
    if (link) link.addEventListener('click', () => jumpToFolder(d.dir));
  }

  function jumpToFolder(dir) {
    try { closeViewer(); } catch (e) {}
    state.folder.stack = [''];
    state.folder.index = 0;
    state.folder.data = null;
    // 立即切到文件夹视图给反馈（skipLoad 阻止 setView 的兜底去加载根目录），
    // 再异步加载目标目录。loadFolder 内部已用 seq 保证「最后一次跳转生效」，
    // 且 st.path 在请求发出前就更新，慢请求/失败期间也不会显示成根目录。
    setView('folder', { skipLoad: true });
    loadFolder(dir, false);
  }

  function toggleSheet() {
    if (state.viewer.sheet) closeSheet();
    else openSheet();
  }
  function openSheet() {
    const item = state.viewer.items[state.viewer.index];
    if (!item) return;
    state.viewer.sheet = true;
    sheet.classList.add('is-open');
    sheet.setAttribute('aria-hidden', 'false');
    viewer.classList.add('sheet-open');
    renderSheet(item);
  }
  function closeSheet(instant) {
    state.viewer.sheet = false;
    sheet.classList.remove('is-open');
    sheet.setAttribute('aria-hidden', 'true');
    viewer.classList.remove('sheet-open');
    sheetToken++;
  }
  $('#vInfo').addEventListener('click', toggleSheet);
  $('#sheetClose').addEventListener('click', closeSheet);
  // 下滑手势关闭详情
  let sheetY = null;
  sheet.addEventListener('touchstart', e => { sheetY = e.touches[0].clientY; }, { passive: true });
  sheet.addEventListener('touchend', e => {
    if (sheetY === null) return;
    const dy = e.changedTouches[0].clientY - sheetY;
    if (dy > 70) closeSheet();
    sheetY = null;
  }, { passive: true });

  /* ---------------- 顶栏按钮 ---------------- */
  /* 换一批：重新随机 + 顺带刷新目录（服务端 20 秒内只重扫一次） */
  $('#btnShuffle').addEventListener('click', () => {
    if (state.view === 'folder') {
      state.folder.data = null;
      loadFolder(state.folder.path, false, true);
      return;
    }
    loadCounts(true);
    loadFeed(state.view, true, true);
  });

  /* ---------------- 启动 ---------------- */
  /** 从超清页回来时（?restore=绝对路径），直接恢复到那张图的大图预览 */
  async function restoreFromQuery() {
    let target = '';
    try { target = new URLSearchParams(location.search).get('restore') || ''; } catch (e) {}
    if (!target) return false;
    try {
      if (history.replaceState) history.replaceState(null, '', location.pathname);
    } catch (e) {}
    const dir = dirOf(target);
    await loadFolder(dir, true);      // 先加载目标目录，再切视图，避免 setView 去加载根目录
    setView('folder');
    const files = (state.folder.data && state.folder.data.files) || [];
    let i = files.findIndex(f => f.path === target);
    if (i < 0) {
      // 路径里有重复分隔符/大小写差异时，退回用文件名匹配
      const name = target.split(/[\\/]/).pop();
      i = files.findIndex(f => f.name === name);
    }
    if (i >= 0) { openViewer('folder', i); return true; }
    return false;
  }

  function boot() {
    document.body.dataset.view = 'photo';
    applyTheme(getTheme());
    setView('photo');
    loadCounts();
    loadFeed('photo', true);
    setupInfinite('photo');
    setupInfinite('video');
    // 文件夹结构在空闲时预取，切过去几乎零等待
    const idle = window.requestIdleCallback || function (fn) { return setTimeout(fn, 1200); };
    idle(() => { api('/api/folder?path=').catch(() => {}); });

    // 从超清页返回时恢复现场
    restoreFromQuery();

    // 从后台切回、窗口尺寸变化、页面从缓存恢复时，都重排一次
    window.addEventListener('load', relayoutAll);
    window.addEventListener('pageshow', () => { applyTheme(getTheme()); relayoutAll(); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) relayoutAll(); });
    let rt = 0;
    window.addEventListener('resize', () => {
      clearTimeout(rt);
      rt = setTimeout(relayoutAll, 120);
    }, { passive: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
