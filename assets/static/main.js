/* ============================================================
   站点交互脚本：主题切换、移动端导航、搜索、目录高亮、代码复制
   ============================================================ */
(function () {
  'use strict';

  var root = document.documentElement;

  /* ---------------- 主题 ---------------- */
  function applyTheme(theme) {
    root.setAttribute('data-theme', theme);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'dark' ? '#0e1116' : '#ffffff');
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.innerHTML = theme === 'dark' ? ICONS.sun : ICONS.moon;
  }

  var ICONS = {
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>',
    menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>'
  };

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem('blog-theme'); } catch (e) {}
    var theme = saved || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    applyTheme(theme);

    var btn = document.getElementById('theme-toggle');
    if (btn) {
      btn.addEventListener('click', function () {
        var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        applyTheme(next);
        try { localStorage.setItem('blog-theme', next); } catch (e) {}
      });
    }
    // 系统主题变化时（未手动指定过）跟随
    if (!saved && window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
        applyTheme(e.matches ? 'dark' : 'light');
      });
    }
  }

  /* ---------------- 移动端导航 ---------------- */
  function initNav() {
    var toggle = document.getElementById('nav-toggle');
    var nav = document.getElementById('site-nav');
    if (!toggle || !nav) return;
    toggle.addEventListener('click', function () {
      var open = nav.classList.toggle('is-open');
      toggle.innerHTML = open ? ICONS.close : ICONS.menu;
      toggle.setAttribute('aria-expanded', String(open));
    });
    nav.addEventListener('click', function (e) {
      if (e.target.tagName === 'A') { nav.classList.remove('is-open'); toggle.innerHTML = ICONS.menu; }
    });
  }

  /* ---------------- 代码复制 ---------------- */
  function initCopy() {
    document.querySelectorAll('.prose .codehilite, .prose pre').forEach(function (block) {
      if (block.querySelector('.copy-btn')) return;
      if (block.parentElement && block.parentElement.classList.contains('codehilite')) return;
      var btn = document.createElement('button');
      btn.className = 'copy-btn';
      btn.type = 'button';
      btn.textContent = '复制';
      btn.addEventListener('click', function () {
        var code = block.querySelector('code');
        var text = code ? code.innerText : block.innerText;
        var done = function () {
          btn.textContent = '已复制';
          btn.classList.add('is-done');
          setTimeout(function () { btn.textContent = '复制'; btn.classList.remove('is-done'); }, 1600);
        };
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(text).then(done).catch(fallback);
        } else { fallback(); }
        function fallback() {
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          try { document.execCommand('copy'); done(); } catch (e) {}
          document.body.removeChild(ta);
        }
      });
      block.appendChild(btn);
    });
  }

  /* ---------------- 目录高亮 + 阅读进度 ---------------- */
  function initTocAndProgress() {
    var tocLinks = Array.prototype.slice.call(document.querySelectorAll('.toc a'));
    var headings = tocLinks.map(function (a) {
      var id = decodeURIComponent(a.getAttribute('href').slice(1));
      return document.getElementById(id);
    }).filter(Boolean);

    var bar = document.getElementById('progress-bar');

    function onScroll() {
      if (bar) {
        var h = document.documentElement.scrollHeight - window.innerHeight;
        var p = h > 0 ? (window.scrollY / h) * 100 : 0;
        bar.style.width = Math.min(100, Math.max(0, p)) + '%';
      }
      if (!headings.length) return;
      var idx = -1;
      for (var i = 0; i < headings.length; i++) {
        if (headings[i].getBoundingClientRect().top <= 110) idx = i; else break;
      }
      tocLinks.forEach(function (a, i) { a.classList.toggle('is-active', i === idx); });
    }

    var ticking = false;
    window.addEventListener('scroll', function () {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(function () { onScroll(); ticking = false; });
    }, { passive: true });
    onScroll();
  }

  /* ---------------- 搜索 ---------------- */
  var searchIndex = null;
  var searchLoading = false;

  function loadIndex(cb) {
    if (searchIndex) return cb(searchIndex);
    if (searchLoading) return;
    searchLoading = true;
    fetch('/search-index.json')
      .then(function (r) { return r.json(); })
      .then(function (data) { searchIndex = data; cb(data); })
      .catch(function () { searchLoading = false; });
  }

  function score(item, q) {
    var t = item.title.toLowerCase();
    var s = (item.summary || '').toLowerCase();
    var g = (item.tags || []).join(' ').toLowerCase();
    var c = (item.plain || '').toLowerCase();
    var n = 0;
    if (t.indexOf(q) === 0) n += 100;
    else if (t.indexOf(q) > -1) n += 60;
    if (g.indexOf(q) > -1) n += 30;
    if (s.indexOf(q) > -1) n += 20;
    if (c.indexOf(q) > -1) n += 8;
    return n;
  }

  function initSearch() {
    var dialog = document.getElementById('search-dialog');
    if (!dialog || typeof dialog.showModal !== 'function') return;
    var input = document.getElementById('search-input');
    var results = document.getElementById('search-results');
    var openers = document.querySelectorAll('[data-open-search]');

    openers.forEach(function (b) {
      b.addEventListener('click', function (e) {
        e.preventDefault();
        dialog.showModal();
        loadIndex(function () {});
        setTimeout(function () { input.focus(); input.select(); }, 30);
      });
    });

    function render(q) {
      q = (q || '').trim().toLowerCase();
      if (!q) {
        results.innerHTML = '<div class="search-empty">输入关键词搜索文章标题、标签或正文</div>';
        return;
      }
      var hits = (searchIndex || [])
        .map(function (it) { return { it: it, s: score(it, q) }; })
        .filter(function (x) { return x.s > 0; })
        .sort(function (a, b) { return b.s - a.s || (a.it.date < b.it.date ? 1 : -1); })
        .slice(0, 20);

      if (!hits.length) {
        results.innerHTML = '<div class="search-empty">没有找到「' + escapeHtml(q) + '」相关内容</div>';
        return;
      }
      results.innerHTML = hits.map(function (x, i) {
        var it = x.it;
        return '<a class="search-hit' + (i === 0 ? ' is-active' : '') + '" href="' + it.url + '">' +
          '<div class="search-hit__title">' + escapeHtml(it.title) + '</div>' +
          '<div class="search-hit__meta">' + it.date + ' · ' + (it.tags || []).map(function (t) { return '#' + t; }).join(' ') + '</div>' +
          '</a>';
      }).join('');
    }

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    }

    input.addEventListener('input', function () { render(input.value); });
    input.addEventListener('keydown', function (e) {
      var items = Array.prototype.slice.call(results.querySelectorAll('.search-hit'));
      if (!items.length) return;
      var cur = items.findIndex(function (el) { return el.classList.contains('is-active'); });
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        var next = e.key === 'ArrowDown' ? Math.min(cur + 1, items.length - 1) : Math.max(cur - 1, 0);
        items.forEach(function (el) { el.classList.remove('is-active'); });
        items[next].classList.add('is-active');
        items[next].scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter') {
        var el = items[Math.max(cur, 0)];
        if (el) window.location.href = el.getAttribute('href');
      }
    });

    dialog.addEventListener('click', function (e) { if (e.target === dialog) dialog.close(); });
    dialog.addEventListener('close', function () { input.value = ''; results.innerHTML = ''; });

    // 快捷键：/ 或 Cmd/Ctrl + K
    document.addEventListener('keydown', function (e) {
      var tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
      if (e.key === '/' || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k')) {
        e.preventDefault();
        if (!dialog.open) { dialog.showModal(); loadIndex(function () {}); setTimeout(function () { input.focus(); }, 30); }
      }
    });

    render('');
  }

  /* ---------------- 回到顶部 ---------------- */
  function initMisc() {
    document.querySelectorAll('[data-top]').forEach(function (el) {
      el.addEventListener('click', function (e) { e.preventDefault(); window.scrollTo({ top: 0, behavior: 'smooth' }); });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initTheme();
    initNav();
    initCopy();
    initTocAndProgress();
    initSearch();
    initMisc();
  });
})();
