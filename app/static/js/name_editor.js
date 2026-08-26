/* name_editor.js — 命名模板编辑器（共享组件）
 * 三页复用同一套交互：设置页（每方案）/ 单作品采集 / 增量采集。
 * 用法：
 *   openNameEditor({
 *     scope: 'single' | 'batch',
 *     getFormat: function(){ return '当前命名代码字符串'; },
 *     setFormat: function(fmt){ 写回并刷新; },
 *     title: '可选标题',
 *     onClose: function(){ 可选 }
 *   });
 * 注意：本组件只负责"编辑命名代码"，命名归存储方案所有，不存在"默认继承"概念。
 */
(function () {
  'use strict';

  // 全局 escapeHtml 兜底（各页均有定义，这里防万一）
  if (typeof window.escapeHtml !== 'function') {
    window.escapeHtml = function (v) {
      return String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    };
  }

  // UI 层字段（合并同名异义）：增量引擎 nickname/desc，单作品 author/title。
  var TPL_UI_FIELDS = [
    ['create_time', '发布时间', 'both'],
    ['type', '作品类型', 'both'],
    ['nickname', '作者', 'both'],     // 增量: nickname；单作品: author
    ['desc', '标题', 'both'],         // 增量: desc；单作品: title
    ['id', '作品ID', 'both'],
    ['uid', '账号UID', 'batch'],
    ['mark', '账号标识', 'batch'],
    ['platform', '平台', 'single']
  ];
  // 各方案实际保存键
  var TPL_ENGINE_KEYS = {
    batch:  { create_time: 'create_time', type: 'type', nickname: 'nickname', desc: 'desc', id: 'id', uid: 'uid', mark: 'mark' },
    single: { create_time: 'create_time', type: 'type', nickname: 'author', desc: 'title', id: 'id', platform: 'platform' }
  };
  var TPL_EXAMPLES = {
    create_time: '2026-08-20_14-30', type: '视频', nickname: '竞品A', desc: '高管专访抢先看',
    id: '7390001', uid: 'MS4wLjAB', mark: 'douyin_739', platform: 'douyin'
  };

  var NE = { scope: null, parts: [], get: null, set: null, onClose: null, title: '', _drag: null };

  function tplSchemeFields(scope) {
    return TPL_UI_FIELDS.filter(function (f) { return f[2] === 'both' || f[2] === scope; })
      .map(function (f) { return f[0]; });
  }

  // 命名代码字符串 → UI 字段顺序
  function parseFormatToParts(fmt, scope) {
    if (!fmt) return [];
    if (scope === 'single') {
      var engMap = { create_time: 'create_time', type: 'type', author: 'nickname', title: 'desc', id: 'id', platform: 'platform' };
      var m = (fmt || '').match(/\{(\w+)\}/g) || [];
      return m.map(function (x) { return engMap[x.slice(1, -1)] || x.slice(1, -1); })
        .filter(function (k) { return tplSchemeFields('single').indexOf(k) !== -1; });
    }
    var uiMap = { create_time: 'create_time', type: 'type', nickname: 'nickname', desc: 'desc', id: 'id', uid: 'uid', mark: 'mark' };
    var valid = tplSchemeFields('batch');
    return (fmt || '').trim().split(/\s+/).filter(Boolean)
      .map(function (k) { return uiMap[k] || k; })
      .filter(function (k) { return valid.indexOf(k) !== -1; });
  }

  // UI 字段顺序 → 命名代码字符串
  function formatFromParts(parts, scope) {
    if (!parts || !parts.length) return '';
    if (scope === 'single') {
      var uiToEng = { create_time: 'create_time', type: 'type', nickname: 'author', desc: 'title', id: 'id', platform: 'platform' };
      return parts.map(function (k) { return uiToEng[k]; }).filter(Boolean)
        .map(function (k) { return '{' + k + '}'; }).join(' ');
    }
    var engMap = TPL_ENGINE_KEYS.batch;
    return parts.map(function (k) { return engMap[k] || k; }).join(' ');
  }

  function openNameEditor(opts) {
    opts = opts || {};
    NE.scope = (opts.scope === 'batch') ? 'batch' : 'single';
    NE.get = opts.getFormat || function () { return ''; };
    NE.set = opts.setFormat || function () {};
    NE.onClose = opts.onClose || null;
    NE.title = opts.title || '编辑命名';
    NE.parts = parseFormatToParts(NE.get(), NE.scope);
    NE._drag = null;
    var tEl = document.getElementById('nm-title-text');
    if (tEl) tEl.textContent = NE.title;
    var modal = document.getElementById('name-modal');
    var mask = document.getElementById('nm-mask');
    if (modal) { modal.classList.add('open'); modal.setAttribute('aria-hidden', 'false'); }
    if (mask) mask.classList.add('show');
    renderNameEditorBody();
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
  }

  function closeNameEditor() {
    var fmt = formatFromParts(NE.parts, NE.scope);
    try { if (NE.set) NE.set(fmt); } catch (e) { if (window.console) console.error('name editor set error:', e); }
    var modal = document.getElementById('name-modal');
    var mask = document.getElementById('nm-mask');
    if (modal) { modal.classList.remove('open'); modal.setAttribute('aria-hidden', 'true'); }
    if (mask) mask.classList.remove('show');
    if (NE.onClose) { try { NE.onClose(); } catch (e) {} }
    NE.scope = null; NE.parts = []; NE.get = null; NE.set = null; NE.onClose = null; NE.title = '';
  }

  function copyNameCode() {
    var code = formatFromParts(NE.parts, NE.scope);
    if (!code) { if (window.showToast) showToast('请先选择字段', 'error'); return; }
    var done = function () { if (window.showToast) showToast('已复制命名代码'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(code).then(done, function () { _neCopyFallback(code); done(); });
    } else { _neCopyFallback(code); done(); }
  }
  function _neCopyFallback(txt) {
    var ta = document.createElement('textarea');
    ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
  }

  function renderNameEditorBody() { renderNePool(); renderNeBelt(); renderNePreview(); }

  function renderNePool() {
    var pool = document.getElementById('wb-pool');
    if (!pool) return;
    var parts = NE.parts;
    pool.innerHTML = TPL_UI_FIELDS.filter(function (f) {
      return f[2] === 'both' || f[2] === NE.scope;
    }).map(function (f) {
      var k = f[0], label = f[1], inP = parts.indexOf(k) !== -1;
      var mini = (f[2] === 'batch') ? '<span class="mini g">仅增量</span>'
        : (f[2] === 'single') ? '<span class="mini v">仅单作品</span>' : '';
      return '<span class="fchip' + (inP ? ' on' : '') + '" data-in="' + inP + '" onclick="NE_toggleField(\'' + k + '\')">' +
        escapeHtml(label) + (inP ? '<span class="mini remove">已加 ✕</span>' : '') + mini + '</span>';
    }).join('');
  }

  function renderNeBelt() {
    var belt = document.getElementById('wb-belt');
    if (!belt) return;
    var parts = NE.parts;
    belt.innerHTML = parts.length ? parts.map(function (key, i) {
      var meta = TPL_UI_FIELDS.find(function (f) { return f[0] === key; });
      var label = meta ? meta[1] : key;
      return '<span class="piece" draggable="true" ' +
        'ondragstart="NE_dragStart(event,' + i + ')" ' +
        'ondragover="event.preventDefault()" ' +
        'ondrop="NE_drop(event,' + i + ')">' +
        escapeHtml(label) +
        '<span class="x" title="移除" onclick="NE_removeField(' + i + ')">×</span></span>' +
        (i < parts.length - 1 ? '<span class="plus">+</span>' : '');
    }).join('') : '<span class="belt-empty">点击上方字段加入命名</span>';
  }

  function renderNePreview() {
    var pv = document.getElementById('wb-pv');
    if (!pv) return;
    var parts = NE.parts;
    pv.innerHTML = '<span class="lab">文件名</span>' + (parts.length ? parts.map(function (key, i) {
      return '<span class="part">' + escapeHtml(TPL_EXAMPLES[key] || ('<' + key + '>')) + '</span>' +
        (i < parts.length - 1 ? '<span class="sep">-</span>' : '');
    }).join('') : '<span class="muted">未选择字段</span>') + '<span class="ext">.mp4</span>';
  }

  // 暴露到全局（inline onclick 需要）
  window.NE_toggleField = function (k) {
    var parts = NE.parts;
    var idx = parts.indexOf(k);
    if (idx === -1) parts.push(k); else parts.splice(idx, 1);
    renderNameEditorBody();
  };
  window.NE_removeField = function (i) { NE.parts.splice(i, 1); renderNameEditorBody(); };
  window.NE_dragStart = function (e, i) {
    NE._drag = i;
    if (e && e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      // Firefox 等浏览器要求 dragstart 必须 setData 才启动拖拽
      try { e.dataTransfer.setData('text/plain', ''); } catch (err) {}
    }
  };
  window.NE_drop = function (e, i) {
    e.preventDefault();
    var d = NE._drag;
    if (d === null || d === undefined || d === i) return;
    var parts = NE.parts;
    var item = parts.splice(d, 1)[0];
    parts.splice(i, 0, item);
    NE._drag = null;
    renderNameEditorBody();
  };

  window.openNameEditor = openNameEditor;
  window.closeNameEditor = closeNameEditor;
  window.copyNameCode = copyNameCode;
})();
