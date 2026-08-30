/* DoukHub 账号作品视图（通用组件，自包含样式）
 * 入口：openAccountWorks({ sec_user_id, name })
 * 使用页：表浏览（账号表行内按钮）、采集页（批次详情账号行）
 */
(function () {
  var AW_CSS = ''
    + '.aw-overlay{position:fixed;inset:0;background:rgba(31,27,23,.45);z-index:2000;display:none;align-items:flex-start;justify-content:center;padding:6vh 16px;backdrop-filter:blur(2px);}'
    + '.aw-overlay.aw-show{display:flex;animation:awFade .16s ease;}'
    + '@keyframes awFade{from{opacity:0}to{opacity:1}}'
    + '.aw-box{background:#fff;border:1px solid #e5e1db;border-radius:12px;box-shadow:0 24px 64px rgba(31,27,23,.20),0 2px 8px rgba(31,27,23,.08);width:min(92vw,760px);max-height:86vh;display:flex;flex-direction:column;animation:awUp .22s cubic-bezier(.16,1,.3,1);}'
    + '@keyframes awUp{from{transform:translateY(14px);opacity:0}to{transform:none;opacity:1}}'
    + '.aw-header{display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid #eee9e2;flex-shrink:0;}'
    + '.aw-title{font-size:16px;font-weight:700;color:#1f1b17;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}'
    + '.aw-close{border:none;background:transparent;cursor:pointer;color:#8a8378;padding:4px;border-radius:6px;display:flex;}'
    + '.aw-close:hover{background:#f4f1ec;color:#1f1b17;}'
    + '.aw-metrics{display:flex;gap:8px;padding:12px 18px 0;flex-wrap:wrap;}'
    + '.aw-metric{border-left:3px solid #d8d2c8;background:#faf8f5;padding:6px 12px;border-radius:0 8px 8px 0;min-width:86px;}'
    + '.aw-metric b{display:block;font-size:18px;color:#1f1b17;line-height:1.2;}'
    + '.aw-metric span{font-size:12px;color:#8a8378;}'
    + '.aw-metric.is-succ{border-left-color:#2e9e5b;}'
    + '.aw-metric.is-fail{border-left-color:#d64545;}'
    + '.aw-metric.is-dup{border-left-color:#e0932f;}'
    + '.aw-toolbar{display:flex;gap:8px;padding:12px 18px;align-items:center;flex-wrap:wrap;}'
    + '.aw-toolbar input,.aw-toolbar select{padding:7px 10px;border:1px solid #e5e1db;border-radius:8px;font-size:13px;background:#fff;color:#1f1b17;outline:none;}'
    + '.aw-toolbar input{flex:1;min-width:140px;}'
    + '.aw-toolbar input:focus,.aw-toolbar select:focus{border-color:#b9b2a5;}'
    + '.aw-dup-toggle{display:flex;align-items:center;gap:6px;font-size:13px;color:#1f1b17;cursor:pointer;user-select:none;}'
    + '.aw-list{flex:1;overflow-y:auto;padding:0 18px 18px;}'
    + '.aw-row{border:1px solid #eee9e2;border-radius:10px;margin-bottom:8px;background:#fff;overflow:hidden;}'
    + '.aw-row.is-fail{border-color:#f3d4d4;}'
    + '.aw-row-main{display:flex;align-items:center;gap:10px;padding:9px 12px;cursor:pointer;}'
    + '.aw-row-main:hover{background:#faf8f5;}'
    + '.aw-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;background:#2e9e5b;}'
    + '.aw-row.is-fail .aw-dot{background:#d64545;}'
    + '.aw-row-title{flex:1;font-size:13px;color:#1f1b17;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}'
    + '.aw-kind{font-size:11px;padding:2px 8px;border-radius:20px;background:#f4f1ec;color:#6f6a60;flex-shrink:0;}'
    + '.aw-dup-tag{font-size:11px;padding:2px 8px;border-radius:20px;background:#fdf3e3;color:#b06f0f;flex-shrink:0;}'
    + '.aw-dup-tag.is-batch{background:#fdeaea;color:#c23b3b;}'
    + '.aw-batch{font-size:11px;color:#8a8378;flex-shrink:0;}'
    + '.aw-detail{display:none;border-top:1px solid #f0ece5;background:#faf8f5;padding:10px 12px;}'
    + '.aw-row.open .aw-detail{display:block;}'
    + '.aw-detail-row{display:flex;gap:8px;font-size:12px;color:#6f6a60;margin-bottom:6px;align-items:baseline;}'
    + '.aw-detail-row b{color:#8a8378;font-weight:600;flex-shrink:0;width:56px;}'
    + '.aw-detail-row span{word-break:break-all;color:#1f1b17;}'
    + '.aw-ops{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;}'
    + '.aw-btn{font-size:12px;padding:5px 12px;border-radius:7px;border:1px solid #e5e1db;background:#fff;color:#1f1b17;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:5px;}'
    + '.aw-btn:hover{background:#f4f1ec;}'
    + '.aw-empty{text-align:center;color:#8a8378;padding:40px 0;font-size:13px;}'
    + '.aw-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1f1b17;color:#fff;font-size:13px;padding:8px 18px;border-radius:8px;z-index:3000;animation:awFade .2s ease;}';

  var styleInjected = false;
  function injectStyle() {
    if (styleInjected) return;
    var el = document.createElement('style');
    el.textContent = AW_CSS;
    document.head.appendChild(el);
    styleInjected = true;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function toast(msg) {
    var t = document.createElement('div');
    t.className = 'aw-toast';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 2200);
  }

  var state = { works: [], filter: { kw: '', st: '', dupOnly: false }, meta: { sec: '', name: '' } };

  function kindLabel(k) { return k === 'image' ? '图集' : (k === 'live' ? '实况' : '视频'); }

  function filtered() {
    return state.works.filter(function (w) {
      var f = state.filter;
      if (f.st === 'success' && w.status !== 'success') return false;
      if (f.st === 'failed' && w.status === 'success') return false;
      if (f.dupOnly) {
        var dupBatch = parseInt(w.dup_in_batch || 0) > 1;
        var dupAll = parseInt(w.dup_total || 0) > 1;
        if (!dupBatch && !dupAll) return false;
      }
      if (f.kw) {
        var hay = (String(w.title || '') + ' ' + String(w.file_name || '') + ' ' + String(w.aweme_id || '')).toLowerCase();
        if (hay.indexOf(f.kw) < 0) return false;
      }
      return true;
    });
  }

  function render() {
    var listEl = document.getElementById('aw-list');
    if (!listEl) return;
    var items = filtered();
    if (!items.length) {
      listEl.innerHTML = '<div class="aw-empty">暂无作品记录（作品明细从新批次开始记录）</div>';
      return;
    }
    listEl.innerHTML = items.map(function (w, i) {
      var ok = w.status === 'success';
      var dupBatch = parseInt(w.dup_in_batch || 0) > 1;
      var dupAll = parseInt(w.dup_total || 0) > 1;
      var dupHtml = dupBatch
        ? '<span class="aw-dup-tag is-batch" title="同批次内重复">重复 ×' + w.dup_in_batch + '</span>'
        : (dupAll ? '<span class="aw-dup-tag" title="跨批次重复采集">已存在 ×' + w.dup_total + '</span>' : '');
      var time = String(w.collected_at || '').replace('T', ' ').slice(0, 16);
      var detail = '<div class="aw-detail">'
        + '<div class="aw-detail-row"><b>作品ID</b><span>' + esc(w.aweme_id || '-') + '</span></div>'
        + '<div class="aw-detail-row"><b>文件名</b><span>' + esc(w.file_name || '未记录') + '</span></div>'
        + '<div class="aw-detail-row"><b>下载目录</b><span>' + esc(w.download_dir || '未记录') + '</span></div>'
        + (ok ? '' : '<div class="aw-detail-row"><b>失败原因</b><span style="color:#c23b3b;">' + esc(w.message || '下载失败') + '</span></div>')
        + '<div class="aw-ops">'
        + '<button class="aw-btn" onclick="AW_open(' + w.id + ',\'file\')"><i data-lucide="file-video"></i>打开文件</button>'
        + '<button class="aw-btn" onclick="AW_open(' + w.id + ',\'dir\')"><i data-lucide="folder-open"></i>打开目录</button>'
        + (w.work_url ? '<a class="aw-btn" href="' + esc(w.work_url) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>原作品</a>' : '')
        + '</div></div>';
      return '<div class="aw-row' + (ok ? '' : ' is-fail') + '" id="aw-row-' + i + '">'
        + '<div class="aw-row-main" onclick="AW_toggle(' + i + ')">'
        + '<span class="aw-dot"></span>'
        + '<span class="aw-row-title" title="' + esc(w.title || w.file_name || w.aweme_id) + '">' + esc(w.title || w.file_name || w.aweme_id || '（无标题）') + '</span>'
        + '<span class="aw-kind">' + kindLabel(w.kind) + '</span>'
        + dupHtml
        + '<span class="aw-batch">' + esc(time) + '</span>'
        + '</div>'
        + detail
        + '</div>';
    }).join('');
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    else if (window.lucide) lucide.createIcons();
  }

  function renderMetrics() {
    var s = state.works;
    var m = {
      total: s.length,
      success: s.filter(function (w) { return w.status === 'success'; }).length,
      failed: s.filter(function (w) { return w.status !== 'success'; }).length,
      dup: s.filter(function (w) { return parseInt(w.dup_in_batch || 0) > 1 || parseInt(w.dup_total || 0) > 1; }).length
    };
    var el = document.getElementById('aw-metrics');
    if (el) el.innerHTML =
      '<div class="aw-metric"><b>' + m.total + '</b><span>作品总数</span></div>'
      + '<div class="aw-metric is-succ"><b>' + m.success + '</b><span>下载成功</span></div>'
      + '<div class="aw-metric is-fail"><b>' + m.failed + '</b><span>失败</span></div>'
      + '<div class="aw-metric is-dup"><b>' + m.dup + '</b><span>重复</span></div>';
  }

  window.AW_toggle = function (i) {
    var row = document.getElementById('aw-row-' + i);
    if (row) row.classList.toggle('open');
  };

  window.AW_open = async function (id, mode) {
    try {
      var r = await fetch('/api/collection/works/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ work_id: id, mode: mode })
      });
      var data = await r.json();
      if (data.success) toast(mode === 'dir' ? '已打开目录' : '已定位文件');
      else toast(data.message || '打开失败', true);
    } catch (e) {
      toast('请求失败: ' + (e.message || e));
    }
  };

  window.AW_filterChange = function () {
    state.filter.kw = (document.getElementById('aw-kw').value || '').trim().toLowerCase();
    state.filter.st = document.getElementById('aw-st').value;
    state.filter.dupOnly = document.getElementById('aw-duponly').checked;
    render();
  };

  window.openAccountWorks = async function (opts) {
    opts = opts || {};
    injectStyle();
    state = { works: [], filter: { kw: '', st: '', dupOnly: false }, meta: { sec: opts.sec_user_id || '', name: opts.name || '' } };
    var old = document.getElementById('aw-overlay-root');
    if (old) old.remove();
    var wrap = document.createElement('div');
    wrap.innerHTML =
      '<div class="aw-overlay aw-show" id="aw-overlay-root">'
      + '<div class="aw-box">'
      + '<div class="aw-header">'
      + '<span class="aw-title" id="aw-title">' + esc(opts.name || opts.sec_user_id || '账号作品') + '</span>'
      + '<button class="aw-close" onclick="AW_close()"><i data-lucide="x"></i></button>'
      + '</div>'
      + '<div class="aw-metrics" id="aw-metrics"></div>'
      + '<div class="aw-toolbar">'
      + '<input id="aw-kw" placeholder="搜索标题 / 文件名 / 作品ID" oninput="AW_filterChange()">'
      + '<select id="aw-st" onchange="AW_filterChange()"><option value="">全部状态</option><option value="success">成功</option><option value="failed">失败</option></select>'
      + '<label class="aw-dup-toggle"><input type="checkbox" id="aw-duponly" onchange="AW_filterChange()">只看重复</label>'
      + '</div>'
      + '<div class="aw-list" id="aw-list"><div class="aw-empty">加载中…</div></div>'
      + '</div></div>';
    document.body.appendChild(wrap);
    wrap.querySelector('.aw-overlay').addEventListener('click', function (e) {
      if (e.target === this) window.AW_close();
    });
    if (window.lucide) lucide.createIcons();

    try {
      var qs = new URLSearchParams();
      if (opts.sec_user_id) qs.set('sec_user_id', opts.sec_user_id);
      if (opts.name && !opts.sec_user_id) qs.set('name', opts.name);
      var resp = await fetch('/api/collection/account-works?' + qs.toString());
      var data = await resp.json();
      state.works = data.works || [];
      renderMetrics();
      render();
    } catch (e) {
      document.getElementById('aw-list').innerHTML = '<div class="aw-empty">加载失败: ' + esc(e.message || '') + '</div>';
    }
  };

  window.AW_close = function () {
    var el = document.getElementById('aw-overlay-root');
    if (el) el.remove();
  };

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') window.AW_close();
  });
})();
