/* collect_detail.js — 单作品采集页面完整交互逻辑 (v7) */

var resolvedSingleLinks = [];
var resolveGeneration = 0;
var currentDownloadMode = 'local';

var singleWorkState = {
    works: [],
    history: [],
    historyFilter: '',
    preferences: JSON.parse(document.getElementById('single-work-preferences').textContent),
    selectedTemplateId: '',
    templateParts: [],
    draggedTemplateIndex: null
};

function invalidateResolvedSingleWorks() {
    resolveGeneration += 1;
    resolvedSingleLinks = [];
}

// ====== Template logic ======
function parseSingleTemplate(template) {
    var parts = [];
    var regex = /\{(\w+)\}/g;
    var last = 0;
    var match;
    while ((match = regex.exec(template)) !== null) {
        if (match.index > last) parts.push({type: 'text', value: template.slice(last, match.index)});
        parts.push({type: 'field', value: match[1]});
        last = regex.lastIndex;
    }
    if (last < template.length) parts.push({type: 'text', value: template.slice(last)});
    return parts;
}

function templatePartsToString() {
    return singleWorkState.templateParts.map(function(p) {
        return p.type === 'field' ? '{' + p.value + '}' : p.value;
    }).join('');
}

function buildFilenamePreview(template, work) {
    if (!work) work = {create_time: '2026-08-15 10-00-00', author: '作者', title: '标题', id: '123', type: '视频', platform: 'douyin'};
    try { return template.format(work); } catch(e) { return template; }
}

String.prototype.format = function(obj) {
    return this.replace(/\{(\w+)\}/g, function(m, key) {
        return obj[key] !== undefined ? String(obj[key]) : m;
    });
};

function renderTemplateBuilder() {
    var container = document.getElementById('template-parts');
    if (!singleWorkState.templateParts.length) {
        container.innerHTML = '<span class="text-muted" style="padding:8px;">拖入字段或点击上方按钮</span>';
        return;
    }
    container.innerHTML = singleWorkState.templateParts.map(function(part, index) {
        var label = part.type === 'field' ? part.value : '"' + escapeHtml(part.value) + '"';
        var icon = part.type === 'field' ? 'code' : 'minus';
        return '<span class="template-part" draggable="true" ondragstart="dragSingleTemplatePart(event,' + index + ')" ondragover="event.preventDefault()" ondrop="dropSingleTemplatePart(event,' + index + ',event.stopPropagation())">' +
            '<i data-lucide="' + icon + '"></i> ' + escapeHtml(label) +
            ' <a href="javascript:void(0)" onclick="removeTemplatePart(' + index + ')" style="margin-left:4px;">×</a></span>';
    }).join('');
    document.getElementById('template-live-preview').textContent = templatePartsToString();
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
}

function dragSingleField(event, key) {
    event.dataTransfer.setData('text/plain', JSON.stringify({type: 'field', value: key}));
}

function dragSingleTemplatePart(event, index) {
    singleWorkState.draggedTemplateIndex = index;
    event.dataTransfer.setData('text/plain', JSON.stringify({type: 'move', index: index}));
}

function dropSingleTemplatePart(event, index, stopPropagation) {
    event.preventDefault();
    if (stopPropagation) event.stopPropagation();
    var raw = event.dataTransfer.getData('text/plain');
    if (!raw) return;
    var data; try { data = JSON.parse(raw); } catch(e) { return; }
    if (!data) return;
    if (data.type === 'move') {
        var from = data.index, to = index < 0 ? singleWorkState.templateParts.length : index;
        if (from === to) return;
        var item = singleWorkState.templateParts.splice(from, 1)[0];
        singleWorkState.templateParts.splice(to, 0, item);
    } else if (data.type === 'field') {
        singleWorkState.templateParts.splice(index < 0 ? singleWorkState.templateParts.length : index, 0, {type: 'field', value: data.value});
    } else if (data.type === 'text') {
        singleWorkState.templateParts.splice(index < 0 ? singleWorkState.templateParts.length : index, 0, {type: 'text', value: data.value});
    }
    renderTemplateBuilder();
}

function appendTemplateField(key) { singleWorkState.templateParts.push({type: 'field', value: key}); renderTemplateBuilder(); }
function appendTemplateSeparator() { var v = document.getElementById('template-separator').value; if (!v) return; singleWorkState.templateParts.push({type: 'text', value: v}); document.getElementById('template-separator').value = ''; renderTemplateBuilder(); }
function removeTemplatePart(index) { singleWorkState.templateParts.splice(index, 1); renderTemplateBuilder(); }

function loadSelectedTemplate() {
    var select = document.getElementById('template-library');
    var tpl = singleWorkState.preferences.templates.find(function(t) { return t.id === select.value; });
    if (!tpl) return;
    document.getElementById('template-name').value = tpl.name;
    singleWorkState.templateParts = parseSingleTemplate(tpl.template);
    singleWorkState.selectedTemplateId = select.value;
    renderTemplateBuilder();
}

function openTemplateModal() {
    var select = document.getElementById('template-library');
    select.innerHTML = singleWorkState.preferences.templates.map(function(t) {
        return '<option value="' + escapeHtml(t.id) + '">' + escapeHtml(t.name) + '</option>';
    }).join('');
    if (singleWorkState.preferences.templates.length) { select.value = singleWorkState.preferences.templates[0].id; loadSelectedTemplate(); }
    document.getElementById('template-modal').style.display = 'flex';
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
}

function closeTemplateModal() { document.getElementById('template-modal').style.display = 'none'; }

async function saveSingleTemplate() {
    var name = document.getElementById('template-name').value.trim();
    if (!name) { showToast('模板名称不能为空', 'error'); return; }
    var template = templatePartsToString();
    var id = singleWorkState.selectedTemplateId || 'new';
    var templates = singleWorkState.preferences.templates.filter(function(t) { return t.id !== id; });
    templates.push({id: id, name: name, template: template, is_default: false});
    try {
        var data = await apiCall('/api/collection/single-work/preferences', 'PUT', {
            download_path: singleWorkState.preferences.download_path,
            recent_dirs: singleWorkState.preferences.recent_dirs,
            default_template_id: singleWorkState.preferences.default_template_id,
            templates: templates,
        });
        if (data.success) { singleWorkState.preferences = data.preferences; renderTemplateSelect(); showToast('模板已保存', 'success'); }
        else { showToast(data.message || '保存失败', 'error'); }
    } catch(error) { showToast(error.message || '保存失败', 'error'); }
}

async function useThisTemplate() {
    var template = templatePartsToString();
    document.getElementById('single-template-input').value = template;
    var select = document.getElementById('single-template-select');
    var name = document.getElementById('template-name').value.trim() || '自定义';
    if (!Array.from(select.options).some(function(o) { return o.value === template; }))
        select.insertAdjacentHTML('beforeend', '<option value="' + escapeHtml(template) + '">' + escapeHtml(name) + '</option>');
    select.value = template;
    updateTemplatePreview();
    closeTemplateModal();
}

function renderTemplateSelect() {
    var select = document.getElementById('single-template-select');
    var prefs = singleWorkState.preferences;
    select.innerHTML = prefs.templates.map(function(t) {
        return '<option value="' + escapeHtml(t.template) + '">' + escapeHtml(t.name) + (t.id === prefs.default_template_id ? ' (默认)' : '') + '</option>';
    }).join('');
    var def = prefs.templates.find(function(t) { return t.id === prefs.default_template_id; });
    if (def) { select.value = def.template; document.getElementById('single-template-input').value = def.template; }
    updateTemplatePreview();
}

function onTemplateSelectChange() {
    document.getElementById('single-template-input').value = document.getElementById('single-template-select').value;
    updateTemplatePreview();
}

function updateTemplatePreview() {
    document.getElementById('single-template-preview').textContent = '预览: ' + buildFilenamePreview(document.getElementById('single-template-input').value, null);
}

function renderRecentDirs() {
    var select = document.getElementById('single-recent-dirs');
    var dirs = singleWorkState.preferences.recent_dirs || [];
    select.innerHTML = '<option value="">最近目录...</option>' + dirs.map(function(d) {
        return '<option value="' + escapeHtml(d) + '">' + escapeHtml(d) + '</option>';
    }).join('');
}

function useRecentSingleDir(path) { if (path) document.getElementById('single-target-dir').value = path; }

async function saveRecentSingleDir(path) {
    if (!path) return;
    var dirs = singleWorkState.preferences.recent_dirs || [];
    dirs = dirs.filter(function(d) { return d !== path; });
    dirs.unshift(path); dirs = dirs.slice(0, 10);
    singleWorkState.preferences.recent_dirs = dirs;
    try { await apiCall('/api/collection/single-work/preferences', 'PUT', { download_path: singleWorkState.preferences.download_path, recent_dirs: dirs, default_template_id: singleWorkState.preferences.default_template_id, templates: singleWorkState.preferences.templates }); } catch(e) {}
    renderRecentDirs();
    updateSettingsSummary();
}

// ====== Inline status ======
function showInlineStatus(text) {
    var box = el('resolve-inline-status');
    var txt = el('resolve-inline-text');
    if (!box || !txt) return;
    box.style.display = 'flex';
    txt.textContent = text;
}

function hideInlineStatus() {
    var box = el('resolve-inline-status');
    if (box) box.style.display = 'none';
}

// ====== SSE progress panel ======
// 分段进度条：解析=2 段（解析链接/获取详情）；下载=3 段（解析链接/下载资产/完成）
function renderSseSegments(container, segCount, active, fail) {
    if (!container) return;
    var html = '';
    for (var i = 0; i < segCount; i++) {
        var cls = 'segment';
        if (i < active) cls += ' done';
        else if (i === active) cls += fail ? ' fail' : ' active';
        html += '<span class="' + cls + '"></span>';
    }
    container.innerHTML = html;
}

function showProgressPanel(title, segCount) {
    segCount = segCount || 2;
    var d = document.getElementById('single-progress');
    d.style.display = 'block';
    d.innerHTML =
        '<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px;">' +
            '<div class="workflow-title"><i data-lucide="activity"></i><span>' + title + '</span></div>' +
            '<span class="workflow-status running" id="sse-status">准备中...</span>' +
        '</div>' +
        '<div class="collect-segments" id="sse-segments"></div>' +
        '<div class="workflow-progress-meta"><span id="sse-text">等待开始...</span><span id="sse-count"></span></div>' +
        '<div class="collect-metrics">' +
            '<div class="workflow-metric" style="border-left-color:var(--dh-accent);"><span class="metric-label">总数</span><span class="metric-value" id="sse-total" style="color:var(--dh-accent);">0</span></div>' +
            '<div class="workflow-metric" style="border-left-color:var(--dh-success);"><span class="metric-label">成功</span><span class="metric-value" id="sse-success" style="color:var(--dh-success);">0</span></div>' +
            '<div class="workflow-metric" style="border-left-color:var(--dh-danger);"><span class="metric-label">失败</span><span class="metric-value" id="sse-failed" style="color:var(--dh-danger);">0</span></div>' +
        '</div>' +
        '<div class="workflow-log" id="sse-log" style="margin-top:10px;max-height:200px;"></div>';
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    renderSseSegments(el('sse-segments'), segCount, 0);
    d.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return { status: el('sse-status'), text: el('sse-text'), count: el('sse-count'), total: el('sse-total'), success: el('sse-success'), failed: el('sse-failed'), log: el('sse-log'), segments: el('sse-segments'), segCount: segCount };
}

function el(id) { return document.getElementById(id); }

function addSseLog(logEl, text, level) {
    var cls = level === 'ok' ? 'log-ok' : (level === 'error' ? 'log-err' : 'log-info');
    var div = document.createElement('div');
    div.className = cls;
    div.textContent = text;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
}

async function consumeSse(resp, els, onProgress, onComplete) {
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';
    while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, {stream: true});
        var lines = buffer.split('\n');
        buffer = lines.pop();
        for (var i = 0; i < lines.length; i++) {
            if (lines[i].indexOf('data: ') !== 0) continue;
            var data; try { data = JSON.parse(lines[i].slice(6)); } catch(e) { continue; }
            if (data.type === 'start') {
                els.status.textContent = '执行中'; els.status.className = 'workflow-status running';
                els.text.textContent = data.message; els.total.textContent = data.total || 0;
                renderSseSegments(els.segments, els.segCount, 0);
                addSseLog(els.log, data.message, 'info');
            } else if (data.type === 'progress') {
                var pct = data.total > 0 ? Math.round((data.index - 1) / data.total * 100) : 0;
                els.text.textContent = data.message; els.count.textContent = pct + '%';
                var phase = data.phase || '';
                var isFail = phase.indexOf('failed') >= 0, isDone = phase.indexOf('done') >= 0;
                els.status.className = 'workflow-status ' + (isFail ? 'failed' : (isDone ? 'success' : 'running'));
                els.status.textContent = isFail ? '失败' : (isDone ? '成功' : '执行中');
                // 分段进度：解析=2 段，下载=3 段；阶段按 phase 推进
                if (phase === 'stage') renderSseSegments(els.segments, els.segCount, 0, isFail);
                else if (phase.indexOf('resolve') >= 0) renderSseSegments(els.segments, els.segCount, 1, isFail);
                else if (phase.indexOf('download') >= 0) renderSseSegments(els.segments, els.segCount, 2, isFail);
                addSseLog(els.log, data.message, isFail ? 'error' : (isDone ? 'ok' : 'info'));
                if (data.success_count !== undefined) els.success.textContent = data.success_count;
                if (data.failed_count !== undefined) els.failed.textContent = data.failed_count;
                if (onProgress) onProgress(data);
            } else if (data.type === 'complete') {
                                els.status.className = 'workflow-status ' + (data.success ? 'success' : 'failed');
                els.status.textContent = data.success ? '已完成' : '失败';
                els.text.textContent = data.message; els.count.textContent = '100%';
                renderSseSegments(els.segments, els.segCount, els.segCount, !data.success);
                if (data.success_count !== undefined) els.success.textContent = data.success_count;
                if (data.failed_count !== undefined) els.failed.textContent = data.failed_count;
                addSseLog(els.log, data.message, data.success ? 'ok' : 'error');
                showToast(data.message, data.success ? 'success' : 'error');
                if (onComplete) onComplete(data);
            }
        }
    }
    hideInlineStatus();
}

// ====== TTD service check ======
async function checkTTD() {
    try {
        var data = await apiCall('/api/services/status', 'GET');
        var ttd = (data.services || []).find(function(s) { return s.name === 'TikTokDownloader'; });
        return Boolean(ttd && ttd.running);
    } catch (e) { return false; }
}

// ====== Utility ======
function formatCount(n) { n = Number(n || 0); return n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n); }

function typeBadge(t) {
    var icon = 'file', cls = 'workflow-status pending';
    if (t === '视频') { icon = 'video'; cls = 'workflow-status success'; }
    else if (t === '图集') { icon = 'images'; cls = 'workflow-status warning'; }
    else if (t === '实况') { icon = 'camera'; cls = 'workflow-status pending'; }
    return '<span class="' + cls + '"><i data-lucide="' + icon + '" style="width:14px;height:14px;"></i> ' + escapeHtml(t) + '</span>';
}

function platformBadge(p) {
    if (p === 'douyin') return '<span class="workflow-status success"><i data-lucide="smartphone" style="width:14px;height:14px;"></i> 抖音</span>';
    if (p === 'tiktok') return '<span class="workflow-status pending"><i data-lucide="music-2" style="width:14px;height:14px;"></i> TikTok</span>';
    return '<span class="workflow-status pending">' + escapeHtml(p) + '</span>';
}

function assetKindLabel(k) { return {video:'视频',image:'图片',live_photo:'实况',music:'音乐',static_cover:'静态封面',dynamic_cover:'动态封面'}[k] || k; }
function assetKindIcon(k) { return {video:'video',image:'image',live_photo:'camera',music:'music',static_cover:'image',dynamic_cover:'film'}[k] || 'file'; }
// 时长格式化：数字（毫秒）→ mm:ss；已是 "00:32" 等格式原样返回
function formatDuration(v) {
    if (!v) return '';
    var s = String(v).trim();
    if (/^\d+$/.test(s)) {
        var sec = Math.round(parseInt(s, 10) / 1000);
        if (sec > 0) return (Math.floor(sec / 60) < 10 ? '0' : '') + Math.floor(sec / 60) + ':' + (sec % 60 < 10 ? '0' : '') + (sec % 60);
    }
    return s;
}
// 资产元信息（时长 · 分辨率 · 大小）；asset 级字段 duration/width/height/size 由后端补充，缺省时降级用作品级 media
function assetMeta(a, media) {
    var parts = [];
    if (a.duration) parts.push(formatDuration(a.duration));
    else if (a.kind === 'video' && media.duration && media.duration !== '00:00:00') parts.push(formatDuration(media.duration));
    if (a.width && a.height) parts.push(a.width + '×' + a.height);
    else if ((a.kind === 'image' || a.kind === 'static_cover' || a.kind === 'dynamic_cover' || a.kind === 'live_photo') && media.width > 0 && media.height > 0) parts.push(media.width + '×' + media.height);
    if (a.size) parts.push(a.size);
    return parts.join(' · ');
}

function copyAssetUrl(url) { if (navigator.clipboard) navigator.clipboard.writeText(url).then(function() { showToast('已复制', 'success'); }); }

// ====== Image Lightbox ======
function openLightbox(url) {
    var box = el('image-lightbox');
    var img = el('lightbox-img');
    if (!box || !img) return;
    img.src = url;
    box.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
}

function closeLightbox() {
    var box = el('image-lightbox');
    if (!box) return;
    box.style.display = 'none';
    el('lightbox-img').src = '';
    document.body.style.overflow = '';
}

// ====== Download mode ======
function setDownloadMode(mode) {
    currentDownloadMode = mode;
    document.querySelectorAll('.dl-choice .choice').forEach(function(btn) {
        btn.classList.toggle('active', btn.dataset.dl === mode);
    });
    document.querySelectorAll('.fb-dl-mode button').forEach(function(btn) {
        btn.classList.toggle('active', btn.dataset.m === mode);
    });
}

function getDownloadMode() { return currentDownloadMode; }

function proxyDownloadAsset(url, filename) {
    var a = document.createElement('a');
    a.href = '/api/collection/works/proxy-download?url=' + encodeURIComponent(url) + '&filename=' + encodeURIComponent(filename || 'download');
    a.download = filename || 'download';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

// ====== Resolve (SSE streaming) ======
async function resolveSingleWorks(event) {
    if (event && event.preventDefault) event.preventDefault();
    if (currentCollectMode !== 'resolve') { switchToResolveMode(); }
    var linksText = String(el('links-input').value || '');
    if (!linksText.trim()) { showToast('请先粘贴作品链接', 'error'); return; }
    var resolveMode = (el('resolve-mode-select') || {}).value || 'auto';
    var btnR = el('detail-resolve'), btnQ = el('quick-download-btn');
    btnR.disabled = true; btnQ.disabled = true;
    btnR.innerHTML = '<i data-lucide="loader-circle"></i><span>解析中...</span>';
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    var els = showProgressPanel('解析进度', 2);
    el('single-work-list').innerHTML = ''; singleWorkState.works = [];
    updateSelection();
    try {
        var resp = await fetch('/api/collection/works/resolve-stream', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({links: linksText, resolve_mode: resolveMode}) });
        await consumeSse(resp, els, function(data) {
            if (data.phase === 'done' && data.work) {
                var existingIdx = singleWorkState.works.findIndex(function(w) { return String(w.id) === String(data.work.id); });
                if (existingIdx < 0) {
                    singleWorkState.works.push(data.work);
                    appendWorkCard(data.work);
                }
                updateSelection();
                if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
            }
        }, function(data) {
            if (data.works && data.works.length) {
                resolvedSingleLinks = data.works.map(function(w) { return w.share_url; }).filter(Boolean);
                singleWorkState.works = data.works; renderSingleWorks(data.works);
                updateSelection();
            }
        });
    } catch (e) {
        els.status.textContent = '请求失败: ' + e.message; els.status.style.color = 'var(--danger)';
        addSseLog(els.log, '请求失败: ' + e.message, 'error'); showToast(e.message || '解析失败', 'error');
    } finally {
        btnR.disabled = false; btnQ.disabled = false;
        btnR.innerHTML = '<i data-lucide="search"></i><span>解析</span>';
        if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    }
}

// ====== Quick Download (fast, no mode selection, local only) ======
async function quickDownload() {
    if (currentCollectMode !== 'quick') { switchToQuickMode(); }
    var linksText = String(el('links-input').value || '');
    if (!linksText.trim()) { showToast('请先粘贴作品链接', 'error'); return; }
    var targetDir = el('single-target-dir').value || '';
    if (!targetDir) { showToast('请先设置保存目录', 'error'); ensureSettingsOpen(); return; }
    var template = el('single-template-input').value || '{create_time} {author} {title}';
    var incMusic = el('opt-music').checked, incSC = el('opt-static-cover').checked, incDC = el('opt-dynamic-cover').checked;
    var btnR = el('detail-resolve'), btnQ = el('quick-download-btn');
    btnR.disabled = true; btnQ.disabled = true;
    btnQ.innerHTML = '<i data-lucide="loader-circle"></i><span>下载中...</span>';
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    var els = showProgressPanel('下载进度', 3);
    el('single-work-list').innerHTML = ''; singleWorkState.works = [];
    updateSelection();
    try {
        var resp = await fetch('/api/collection/works/download-stream', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ links: linksText, target_dir: targetDir, filename_template: template, include_music: incMusic, include_static_cover: incSC, include_dynamic_cover: incDC }) });
        await consumeSse(resp, els, function(data) {
            if (data.phase === 'resolve_done' && data.work) { singleWorkState.works.push(data.work); appendWorkCard(data.work); }
            if (data.phase === 'download_done') markWorkDownloaded(data.title, data.files);
            if (data.phase === 'download_failed') markWorkFailed(data.message);
        }, async function(data) { await saveRecentSingleDir(targetDir); await loadSingleWorkHistory(); });
    } catch (e) {
        els.status.textContent = '请求失败: ' + e.message; els.status.style.color = 'var(--danger)';
        addSseLog(els.log, '请求失败: ' + e.message, 'error'); showToast(e.message || '下载失败', 'error');
    } finally {
        btnR.disabled = false; btnQ.disabled = false;
        btnQ.innerHTML = '<i data-lucide="zap"></i><span>一键下载</span>';
        if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    }
}

function findWorkById(workId) {
    return singleWorkState.works.find(function(w) { return String(w.id) === String(workId); });
}

function findWorkByShareUrl(shareUrl) {
    return singleWorkState.works.find(function(w) { return w.share_url === shareUrl; });
}

// ====== 下载进度辅助（解析结果下载时展示，复用进度面板） ======
var batchEls = null; // downloadAllSelected 跨作品共享面板

function startDownloadProgress(n, total) {
    var els = showProgressPanel('下载进度', 3);
    els.total.textContent = total !== undefined ? total : n;
    els.text.textContent = '正在下载 ' + n + ' 个资产...';
    renderSseSegments(els.segments, 3, 1);
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    return els;
}

function updateDownloadProgress(els, ok, msg) {
    if (!els) return;
    if (ok) {
        els.status.className = 'workflow-status success'; els.status.textContent = '成功';
        els.success.textContent = (parseInt(els.success.textContent) || 0) + 1;
        renderSseSegments(els.segments, 3, 3, false);
    } else {
        els.status.className = 'workflow-status failed'; els.status.textContent = '失败';
        els.failed.textContent = (parseInt(els.failed.textContent) || 0) + 1;
        renderSseSegments(els.segments, 3, 1, true);
    }
    els.text.textContent = msg || (ok ? '下载完成' : '下载失败');
    addSseLog(els.log, msg || (ok ? '下载完成' : '下载失败'), ok ? 'ok' : 'error');
}

// ====== Single asset download ======
async function downloadSingleAsset(link, assetIndex) {
    doDownloadSingleAsset(link, assetIndex);
}

async function doDownloadSingleAsset(link, assetIndex, els) {
    var dlMode = getDownloadMode();
    var work = findWorkByShareUrl(link);
    var asset = work ? (work.assets || []).find(function(a) { return a.index === assetIndex; }) : null;
    if (!asset) { showToast('未找到资产', 'error'); return; }
    if (dlMode === 'browser') {
        var fname = asset.kind;
        try { fname = (work.author || '') + ' ' + (work.title || asset.kind); } catch(e) {}
        proxyDownloadAsset(asset.url, fname);
        showToast('已触发浏览器下载: ' + assetKindLabel(asset.kind) + '，进度见浏览器下载栏', 'success');
        return;
    }
    var els2 = els || startDownloadProgress(1, 1);
    var targetDir = el('single-target-dir').value || '';
    if (!targetDir) { showToast('请先选择保存目录', 'error'); ensureSettingsOpen(); return; }
    var template = el('single-template-input').value || '{create_time} {author} {title}';
    try {
        var data = await apiCall('/api/collection/works/download', 'POST', { links: link, target_dir: targetDir, filename_template: template, asset_indexes: [assetIndex], work: work });
        if (data.success) { updateDownloadProgress(els2, true, '下载成功: ' + assetKindLabel(asset.kind)); showToast('下载成功', 'success'); }
        else { updateDownloadProgress(els2, false, data.message || '下载失败'); showToast('下载失败', 'error'); }
        await loadSingleWorkHistory();
    } catch (error) { updateDownloadProgress(els2, false, error.message || '下载失败'); showToast(error.message || '下载失败', 'error'); }
}

// ====== Asset selection & batch download ======
function onAssetCheckboxChange(workId, assetIndex, checked) {
    var row = document.querySelector('.work-assets[data-work-id="' + workId + '"] .work-asset-row[data-asset-index="' + assetIndex + '"]');
    if (row) row.classList.toggle('selected', checked);
    updateSelection();
}

// 网格视图：点击整格 = 切换勾选；列表视图：保持原行为（点击缩略图放大）
function onAssetCellClick(event, workId, assetIndex) {
    var row = event.currentTarget;
    var container = row.closest('.work-assets');
    if (!container || !container.classList.contains('grid-view')) return;
    if (event.target.closest('.asset-checkbox') || event.target.closest('.asset-actions')) return; // 勾选框/操作按钮不重复触发
    var box = row.querySelector('.asset-checkbox');
    if (box) { box.checked = !box.checked; onAssetCheckboxChange(workId, assetIndex, box.checked); }
}
// 缩略图双击才放大（列表/网格统一）；单击不响应

function toggleAllAssets(btn, workId) {
    var container = document.querySelector('.work-assets[data-work-id="' + workId + '"]');
    if (!container) return;
    var boxes = container.querySelectorAll('.asset-checkbox');
    var allChecked = Array.from(boxes).every(function(b) { return b.checked; });
    boxes.forEach(function(b) {
        b.checked = !allChecked;
        var row = b.closest('.work-asset-row');
        if (row) row.classList.toggle('selected', b.checked);
    });
    updateSelection();
}

function selectAssetsByKind(workId, kind) {
    var container = document.querySelector('.work-assets[data-work-id="' + workId + '"]');
    if (!container) return;
    var boxes = container.querySelectorAll('.asset-checkbox[data-asset-kind="' + kind + '"]');
    var allChecked = Array.from(boxes).every(function(b) { return b.checked; });
    boxes.forEach(function(b) {
        b.checked = !allChecked;
        var row = b.closest('.work-asset-row');
        if (row) row.classList.toggle('selected', b.checked);
    });
    updateSelection();
}

function getSelectedAssetIndexes(workId) {
    var container = document.querySelector('.work-assets[data-work-id="' + workId + '"]');
    if (!container) return [];
    var selected = [];
    container.querySelectorAll('.asset-checkbox:checked').forEach(function(b) {
        selected.push(parseInt(b.dataset.assetIndex));
    });
    return selected;
}

async function downloadSelectedAssets(workId, shareUrl) {
    var indexes = getSelectedAssetIndexes(workId);
    if (!indexes.length) { showToast('请先勾选要下载的资产', 'error'); return; }
    doDownloadSelectedAssets(workId, shareUrl, indexes);
}

async function doDownloadSelectedAssets(workId, shareUrl, indexes, els) {
    var dlMode = getDownloadMode();
    var work = findWorkById(workId);
    if (dlMode === 'browser') {
        var delay = 0;
        (work.assets || []).forEach(function(a) {
            if (indexes.indexOf(a.index) >= 0) {
                setTimeout(function() {
                    var fname = (work.author || '') + ' ' + (work.title || a.kind) + (indexes.length > 1 ? '_' + a.index : '');
                    proxyDownloadAsset(a.url, fname);
                }, delay);
                delay += 800;
            }
        });
        showToast('已触发浏览器下载 ' + indexes.length + ' 个资产，进度见浏览器下载栏', 'success');
        return;
    }
    var els2 = els || startDownloadProgress(indexes.length, indexes.length);
    var targetDir = el('single-target-dir').value || '';
    if (!targetDir) { showToast('请先选择保存目录', 'error'); ensureSettingsOpen(); return; }
    var template = el('single-template-input').value || '{create_time} {author} {title}';
    try {
        var data = await apiCall('/api/collection/works/download', 'POST', {
            links: shareUrl, target_dir: targetDir, filename_template: template, asset_indexes: indexes, work: work
        });
        if (data.success) { updateDownloadProgress(els2, true, '下载 ' + indexes.length + ' 个资产成功'); showToast('下载 ' + indexes.length + ' 个资产成功', 'success'); }
        else { updateDownloadProgress(els2, false, data.message || '下载失败'); showToast('下载失败', 'error'); }
        await loadSingleWorkHistory();
    } catch (error) { updateDownloadProgress(els2, false, error.message || '下载失败'); showToast(error.message || '下载失败', 'error'); }
}

// ====== Work card rendering ======
function buildWorkCardHtml(work) {
    var assets = work.assets || [];
    var stats = work.stats || {};
    var media = work.media || {};
    var music = work.music || {};
    var cover = work.static_cover || '';
    var authorInfo = work.author_info || {};
    var workType = work.type || '';
    var authorName = work.author || authorInfo.nickname || '未知';
    var AUX_KINDS = ['music', 'static_cover', 'dynamic_cover'];
    var mainAssets = assets.filter(function(a) { return AUX_KINDS.indexOf(a.kind) < 0; });
    var auxAssets = assets.filter(function(a) { return AUX_KINDS.indexOf(a.kind) >= 0; });
    var idx = singleWorkState.works.indexOf(work) + 1 || 1;

    var html = '<div class="work-card" data-work-id="' + escapeHtml(work.id) + '">';

    // 头部：点击整行折叠/展开整个作品（chevron 仅为状态图标）
    html += '<div class="wc-head" onclick="toggleWorkCollapsed(\'' + work.id + '\')" style="cursor:pointer;" title="点击折叠/展开整个作品">';
    html += '<span class="wc-collapse"><i data-lucide="chevron-down"></i></span>';
    html += '<span class="wc-idx">' + idx + '</span>';
    html += '<h2>作品 ' + idx + '</h2>';
    html += '<span class="wsub">' + escapeHtml(workType) + ' · ' + assets.length + ' 个资产</span>';
    html += '</div>';

    // 上下文条（原型 workbar）：小封面 + 作者/平台 + 互动/日期 + 描述 + 录入分享表
    html += '<div class="workbar">';
    if (cover) {
        html += '<img class="cover-sm" src="' + escapeHtml(cover) + '" loading="lazy" alt="封面" onclick="openLightbox(\'' + cover.replace(/'/g, "\\'") + '\')" style="cursor:zoom-in;">';
    } else {
        html += '<div class="cover-sm cover-sm-ph"><i data-lucide="' + (workType === '视频' ? 'video' : 'images') + '"></i></div>';
    }
    html += '<div class="wmeta">';
    html += '<div class="wtop"><span class="aname">' + escapeHtml(authorName) + '</span>' + platformBadge(work.platform) + '</div>';
    html += '<div class="wstats">';
    html += '<span>赞 <b>' + formatCount(stats.digg_count) + '</b></span>';
    html += '<span>藏 <b>' + formatCount(stats.collect_count) + '</b></span>';
    html += '<span>评 <b>' + formatCount(stats.comment_count) + '</b></span>';
    html += '<span>转 <b>' + formatCount(stats.share_count) + '</b></span>';
    html += '</div>';
    // 媒体信息组：时长 · 分辨率 · 日期
    var wmediaParts = [];
    if (media.duration && media.duration !== '00:00:00') wmediaParts.push('<i data-lucide="clock"></i>' + formatDuration(media.duration));
    if (media.width > 0 && media.height > 0) wmediaParts.push('<i data-lucide="ratio"></i>' + media.width + '×' + media.height);
    if (work.create_time) wmediaParts.push('<i data-lucide="calendar"></i>' + escapeHtml(work.create_time));
    if (wmediaParts.length) html += '<div class="wmedia">' + wmediaParts.join('<span class="sep">·</span>') + '</div>';
    // 音乐组（BGM）
    if (music.title) {
        html += '<div class="wmusic"><i data-lucide="music"></i><span>' + escapeHtml(music.title) + (music.author ? ' - ' + escapeHtml(music.author) : '') + '</span></div>';
    }
    // 标签组（#话题）
    var tags = (work.hashtags || []).concat(work.video_tags || []);
    if (tags.length) {
        html += '<div class="wtags">' + tags.map(function(tag) {
            var name = (typeof tag === 'object' && tag !== null) ? (tag.hashtag_name || '') : String(tag);
            return name ? '<span class="work-tag">#' + escapeHtml(name) + '</span>' : '';
        }).join('') + '</div>';
    }
    var desc = work.desc || work.title || '';
    if (desc && desc !== work.id) html += '<div class="wdesc">' + escapeHtml(desc) + '</div>';
    html += '</div>';
    html += '<div class="wright"><button type="button" class="btn-share" onclick="addToShareTable(\'' + work.id + '\')" title="将此账号录入分享表"><i data-lucide="user-plus"></i>录入分享表</button></div>';
    html += '</div>';

    // 工具栏（原型 wtb）：计数 + 类型筛选 + 全选 + 视图切换
    if (assets.length) {
        var kindGroups = {};
        assets.forEach(function(a) {
            if (!kindGroups[a.kind]) kindGroups[a.kind] = [];
            kindGroups[a.kind].push(a.index);
        });
        var kindKeys = Object.keys(kindGroups);

        html += '<div class="work-asset-toolbar">';
        html += '<span class="toolbar-count">' + assets.length + ' 个资产</span>';
        // 类型筛选按钮：主类型高亮（kind-filter）/ 附属虚线（aux-filter-btn）
        var MAIN_KINDS = ['video', 'live_photo', 'image'];
        var primaryKind = null;
        MAIN_KINDS.forEach(function(k) {
            if (!primaryKind && kindGroups[k] && kindGroups[k].length) primaryKind = k;
        });
        kindKeys.forEach(function(k) {
            var isAuxKind = AUX_KINDS.indexOf(k) >= 0;
            var show = kindGroups[k].length > 1 || (!isAuxKind && kindKeys.length > 1);
            if (show) {
                var isPrimary = (k === primaryKind);
                html += '<button type="button" class="toolbar-btn' + (isPrimary ? ' kind-filter' : '') + (isAuxKind ? ' aux-filter-btn' : '') + '" onclick="selectAssetsByKind(\'' + work.id + '\',\'' + k + '\')"><i data-lucide="' + assetKindIcon(k) + '"></i> ' + escapeHtml(assetKindLabel(k)) + ' <b class="kind-count">' + kindGroups[k].length + '</b></button>';
            }
        });
        html += '<button type="button" class="toolbar-btn" onclick="toggleAllAssets(this,\'' + work.id + '\')"><i data-lucide="check-square"></i> 全选</button>';
        html += '<button type="button" class="toolbar-btn view-toggle" onclick="toggleAssetView(\'' + work.id + '\')" title="切换列表/网格视图" data-work-id="' + escapeHtml(work.id) + '"><i data-lucide="layout-grid"></i></button>';
        html += '</div>';

        // 资产行：缩略图（视频带时长角标）+ 类型徽章 + 名称 + 元信息 + 已下载 + hover 操作
        function assetRowHtml(a, extraCls) {
            var thumb = a.cover_url || '';
            if (!thumb && (a.kind === 'image' || a.kind === 'static_cover' || a.kind === 'dynamic_cover' || a.kind === 'live_photo')) {
                thumb = a.url;
            }
            var h = '<div class="work-asset-row' + (extraCls ? ' ' + extraCls : '') + '" data-asset-index="' + a.index + '" onclick="onAssetCellClick(event, \'' + work.id + '\',' + a.index + ')">';
            h += '<input type="checkbox" class="asset-checkbox" data-work-id="' + escapeHtml(work.id) + '" data-asset-index="' + a.index + '" data-asset-kind="' + escapeHtml(a.kind) + '" onchange="onAssetCheckboxChange(\'' + work.id + '\',' + a.index + ', this.checked)">';
            if (thumb) {
                var dur = (a.kind === 'video' && media.duration && media.duration !== '00:00:00') ? formatDuration(media.duration) : '';
                h += '<div class="asset-thumb' + (a.kind === 'video' ? ' vid' : '') + '"' + (dur ? ' data-dur="' + escapeHtml(dur) + '"' : '') + ' ondblclick="openLightbox(\'' + thumb.replace(/'/g, "\\'") + '\')" title="双击放大预览" style="cursor:zoom-in;"><img src="' + escapeHtml(thumb) + '" loading="lazy" alt="预览"></div>';
            } else {
                h += '<div class="asset-thumb"><i data-lucide="' + assetKindIcon(a.kind) + '"></i></div>';
            }
            h += '<span class="asset-kind-badge ' + escapeHtml(a.kind) + '">' + escapeHtml(assetKindLabel(a.kind)) + '</span>';
            h += '<div class="asset-info"><span class="asset-name">' + escapeHtml(assetKindLabel(a.kind)) + (assets.length > 1 ? ' ' + a.index : '') + '</span><span class="asset-meta">' + escapeHtml(assetMeta(a, media)) + '</span></div>';
            if (a.downloaded) h += '<span class="asset-done"><i data-lucide="check"></i>已下载</span>';
            h += '<div class="asset-actions">';
            h += '<button type="button" title="复制链接" onclick="copyAssetUrl(\'' + a.url.replace(/'/g, "\\'") + '\')"><i data-lucide="copy"></i></button>';
            h += '<a href="' + escapeHtml(a.url) + '" target="_blank" title="打开"><i data-lucide="external-link"></i></a>';
            h += '<button type="button" class="dl" title="下载此项" onclick="downloadSingleAsset(\'' + work.share_url.replace(/'/g, "\\'") + '\',' + a.index + ')"><i data-lucide="download"></i></button>';
            h += '</div></div>';
            return h;
        }
        html += '<div class="work-assets" data-work-id="' + escapeHtml(work.id) + '">';
        mainAssets.forEach(function(a) { html += assetRowHtml(a); });
        if (auxAssets.length) {
            var labelParts = [];
            if (auxAssets.some(function(a) { return a.kind === 'music'; })) labelParts.push('音乐');
            if (auxAssets.some(function(a) { return a.kind === 'static_cover' || a.kind === 'dynamic_cover'; })) labelParts.push('封面');
            html += '<div class="asset-aux-toggle" data-work-id="' + escapeHtml(work.id) + '" onclick="toggleAuxAssets(\'' + work.id + '\')">';
            html += '<i data-lucide="chevron-right"></i><span>' + (labelParts.join(' · ') || '附属文件') + '</span>';
            html += '<span class="aux-count">' + auxAssets.length + ' 项</span></div>';
            html += '<div class="asset-aux-group" data-work-id="' + escapeHtml(work.id) + '" style="display:none;">';
            auxAssets.forEach(function(a) { html += assetRowHtml(a); });
            html += '</div>';
        }
        html += '</div>';
    }

    // Download status area
    html += '<div class="work-download-status-area" data-work-title="' + escapeHtml(work.title || work.id) + '"></div>';

    html += '</div>'; // work-card
    return html;
}

function appendWorkCard(work) {
    var container = el('single-work-list');
    var card = document.createElement('div');
    card.innerHTML = buildWorkCardHtml(work);
    container.appendChild(card.firstChild);
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
}

function renderSingleWorks(works) {
    el('single-work-list').innerHTML = works.map(function(work) { return buildWorkCardHtml(work); }).join('');
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
}

function markWorkDownloaded(title, files) {
    var area = document.querySelector('.work-download-status-area[data-work-title="' + cssEscape(title) + '"]');
    if (area) {
        area.innerHTML = '<div class="work-download-status success"><i data-lucide="check-circle"></i> 已下载 ' + (files ? files.length : 0) + ' 个文件</div>';
        if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    }
}

function markWorkFailed(msg) {
    var areas = document.querySelectorAll('.work-download-status-area');
    if (areas.length) {
        var last = areas[areas.length - 1];
        last.innerHTML = '<div class="work-download-status failed"><i data-lucide="x-circle"></i> ' + escapeHtml(msg || '下载失败') + '</div>';
        if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    }
}

function cssEscape(s) { return String(s || '').replace(/"/g, '\\"'); }

// ====== History panel toggle ======
function toggleHistoryPanel() {
    var body = el('history-collapsible-body');
    var icon = el('history-collapse-icon');
    if (!body) return;
    var isOpen = body.style.display !== 'none';
    body.style.display = isOpen ? 'none' : 'block';
    if (icon) icon.style.transform = isOpen ? 'rotate(0deg)' : 'rotate(180deg)';
}

// ====== Download history ======
function safeParseJsonArray(v) { try { var p = JSON.parse(v); return Array.isArray(p) ? p : []; } catch(e) { return []; } }

async function loadSingleWorkHistory() {
    try { var data = await apiCall('/api/collection/works/history', 'GET'); singleWorkState.history = data.history || []; renderSingleWorkHistory(); } catch(e) {}
}

function onHistoryFilter() { singleWorkState.historyFilter = el('history-filter').value; renderSingleWorkHistory(); }

function renderSingleWorkHistory() {
    var rows = singleWorkState.history;
    var filter = singleWorkState.historyFilter;
    if (filter) rows = rows.filter(function(r) { return r.status === filter; });
    el('history-count').textContent = rows.length;
    var container = el('single-history-list');
    if (!rows.length) { container.innerHTML = '<div class="text-muted">' + (filter ? '无匹配记录' : '暂无下载记录') + '</div>'; return; }
    container.innerHTML = rows.map(function(row) {
        var files = safeParseJsonArray(row.files_json);
        var work = safeParseJson(row.work_json);
        var isOk = row.status === 'success';
        var sub = isOk
            ? (files.length + ' 个文件 · ' + escapeHtml(row.work_type || ''))
            : escapeHtml(row.error || '未知错误');
        // 增强信息：作者 + 平台 + 目录
        var metaLine = '';
        if (row.author || row.platform || row.target_dir) {
            var metaParts = [];
            if (row.author) metaParts.push(escapeHtml(row.author));
            if (row.platform) metaParts.push(escapeHtml(row.platform));
            if (row.target_dir) metaParts.push('📁 ' + escapeHtml(shortenPath(row.target_dir)));
            metaLine = '<div class="hist-meta">' + metaParts.join(' · ') + '</div>';
        }
        // 原始链接
        var linkLine = '';
        if (row.source_link) {
            linkLine = '<div class="hist-link" title="' + escapeHtml(row.source_link) + '">' +
                '<i data-lucide="link" style="width:11px;height:11px;"></i> ' +
                escapeHtml(truncateUrl(row.source_link)) +
                ' <button type="button" class="hist-copy-btn" onclick="event.stopPropagation();copyHistoryLink(\'' + row.source_link.replace(/'/g, "\\'") + '\')" title="复制链接"><i data-lucide="copy" style="width:11px;height:11px;"></i></button>' +
                '</div>';
        }
        // 操作按钮
        var actions = '<div class="hist-actions">';
        if (!isOk) {
            actions += '<button type="button" class="btn btn-secondary hist-action-btn" onclick="retrySingleWorkHistory(' + row.id + ')"><i data-lucide="refresh-cw" style="width:12px;height:12px;"></i> 重试</button>';
        }
        if (isOk && files.length && row.target_dir) {
            actions += '<button type="button" class="btn btn-secondary hist-action-btn" onclick="openHistoryDir(' + row.id + ')"><i data-lucide="folder-open" style="width:12px;height:12px;"></i> 打开目录</button>';
        }
        actions += '</div>';
        return '<div class="history-card">' +
            '<div class="hist-icon ' + (isOk ? 'success' : 'failed') + '"><i data-lucide="' + (isOk ? 'check' : 'x') + '"></i></div>' +
            '<div class="hist-body">' +
                '<div class="hist-title">' + escapeHtml(row.title || row.work_id) + '</div>' +
                '<div class="hist-sub">' + sub + '</div>' +
                metaLine + linkLine +
            '</div>' +
            '<span class="hist-time">' + formatDateTime(row.created_at) + '</span>' +
            actions +
        '</div>';
    }).join('');
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
}

async function retrySingleWorkHistory(historyId) {
    var targetDir = el('single-target-dir').value || '';
    var button = event.target.closest('button'); button.disabled = true;
    try {
        var data = await apiCall('/api/collection/works/history/' + historyId + '/retry', 'POST', { target_dir: targetDir });
        showToast(data.success ? '重试成功' : '重试失败', data.success ? 'success' : 'error');
        await loadSingleWorkHistory();
    } catch(e) { showToast(e.message || '重试失败', 'error'); }
    finally { button.disabled = false; }
}

// ====== Directory picker ======
var singleDirCurrent = '', singleDirEntries = [], singleDirParent = '';

async function openSingleDirDialog() { await loadSingleDirs(el('single-target-dir').value || ''); el('single-dir-modal').style.display = 'flex'; }

async function loadSingleDirs(path) {
    var data = await apiCall('/api/browse-dir?path=' + encodeURIComponent(path || ''), 'GET');
    singleDirCurrent = data.current || ''; singleDirParent = data.parent || ''; singleDirEntries = data.dirs || [];
    el('single-dir-current').value = singleDirCurrent || '此电脑'; renderSingleDirs();
}

function renderSingleDirs() {
    var list = el('single-dir-list');
    if (!singleDirEntries.length) { list.innerHTML = '<div class="text-muted" style="padding:12px;">没有子目录</div>'; return; }
    list.innerHTML = singleDirEntries.map(function(dir) {
        var name = dir.split(/[\\/]/).filter(Boolean).pop() || dir;
        return '<div style="padding:8px 10px;border-bottom:1px solid var(--border-default);cursor:pointer;">' +
            '<a href="javascript:void(0)" onclick="loadSingleDirs(\'' + dir.replace(/\\/g, '\\\\').replace(/'/g, "\\'") + '\')"><i data-lucide="folder"></i> ' + escapeHtml(name) + '</a></div>';
    }).join('');
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
}

async function goSingleDirParent() { if (singleDirParent && singleDirParent !== singleDirCurrent) await loadSingleDirs(singleDirParent); }
function closeSingleDirDialog() { el('single-dir-modal').style.display = 'none'; }
function chooseSingleDir() { if (singleDirCurrent) el('single-target-dir').value = singleDirCurrent; closeSingleDirDialog(); }

// ====== Misc ======
function formatDateTime(v) { return v ? String(v).replace('T',' ').slice(0,19).replace(/ (\d\d)-(\d\d)-(\d\d)$/, ' $1:$2:$3') : '-'; }

function escapeHtml(v) { return String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;'); }

// ====== Mode switching (解析 / 一键下载 / API请求) ======
var currentCollectMode = 'resolve'; // 'resolve' | 'quick' | 'api'

function switchToApiMode() {
    currentCollectMode = 'api';
    closePop('api-popover');
    applyModeSettings('api');
    showPop('api-popover');
    loadApiInfo();
}

function switchToResolveMode() {
    currentCollectMode = 'resolve';
    closePop('api-popover');
    applyModeSettings('resolve');
}

function switchToQuickMode() {
    currentCollectMode = 'quick';
    closePop('api-popover');
    applyModeSettings('quick');
}

// ====== 按模式启用/禁用设置项 ======
function applyModeSettings(mode) {
    var resolveSelect = el('resolve-mode-select');
    var optMusic = el('opt-music');
    var optStaticCover = el('opt-static-cover');
    var optDynamicCover = el('opt-dynamic-cover');
    var chips = [optMusic, optStaticCover, optDynamicCover];

    function setChips(disabled) {
        chips.forEach(function(cb) {
            if (!cb) return;
            cb.disabled = disabled;
            var chip = cb.closest('.chip');
            if (chip) chip.classList.toggle('disabled', disabled);
        });
    }

    if (mode === 'resolve') {
        // 解析：解析模式选择器可选，附带下载禁用（解析后手动勾选）
        if (resolveSelect) resolveSelect.disabled = false;
        setChips(true);
    } else if (mode === 'quick') {
        // 一键下载：解析模式禁用（默认auto），附带下载可选
        if (resolveSelect) resolveSelect.disabled = true;
        setChips(false);
    } else if (mode === 'api') {
        // API：解析模式可选，附带下载禁用（通过API参数传）
        if (resolveSelect) resolveSelect.disabled = false;
        setChips(true);
    }
}

// ====== API 信息（渲染到悬浮提示浮层） ======
async function loadApiInfo() {
    var contentEl = el('api-pop-content');
    if (!contentEl) return;
    try {
        var data = await apiCall('/api/v1/api-info', 'GET');
        if (!data.enabled) {
            contentEl.innerHTML =
                '<div class="collect-empty-state" style="padding:20px;">' +
                '<div class="collect-empty-icon"><i data-lucide="webhook"></i></div>' +
                '<div class="collect-empty-title">API 模式未启用</div>' +
                '<div class="collect-empty-desc">请在 <a href="/settings" style="color:var(--dh-accent);">设置</a> 中开启 API 请求模式并生成 API Key</div>' +
                '</div>';
            if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
            return;
        }
        var baseUrl = data.base_url || 'http://localhost:2999';
        var apiKey = data.api_key || '';
        var endpoints = data.endpoints || {};
        contentEl.innerHTML =
            '<div style="padding:2px 4px;">' +
            '<div class="form-group">' +
                '<label><i data-lucide="link"></i> API 地址</label>' +
                '<div style="display:flex;gap:8px;align-items:center;">' +
                    '<input type="text" readonly value="' + escapeHtml(baseUrl) + '" style="flex:1;font-family:var(--font-mono);font-size:13px;">' +
                    '<button type="button" class="btn btn-secondary" onclick="copyText(\'' + escapeHtml(baseUrl) + '\')"><i data-lucide="copy"></i> 复制</button>' +
                '</div>' +
            '</div>' +
            '<div class="form-group">' +
                '<label><i data-lucide="key"></i> API Key</label>' +
                '<div style="display:flex;gap:8px;align-items:center;">' +
                    '<input type="text" readonly value="' + escapeHtml(apiKey) + '" style="flex:1;font-family:var(--font-mono);font-size:13px;">' +
                    '<button type="button" class="btn btn-secondary" onclick="copyText(\'' + escapeHtml(apiKey) + '\')"><i data-lucide="copy"></i> 复制</button>' +
                '</div>' +
            '</div>' +
            '<div class="form-group">' +
                '<label><i data-lucide="terminal"></i> 接口列表</label>' +
                '<div style="font-family:var(--font-mono);font-size:12px;background:var(--dh-surface-muted);padding:12px;border-radius:var(--dh-radius-sm);border:1px solid var(--dh-border);line-height:1.8;">' +
                    '<strong>POST</strong> ' + escapeHtml(endpoints.resolve || '') + ' — 解析链接，返回下载地址<br>' +
                    '<strong>POST</strong> ' + escapeHtml(endpoints.download || '') + ' — 解析+本地下载<br>' +
                    '<strong>GET</strong>&nbsp; ' + escapeHtml(endpoints.status || '') + ' — 检查服务状态<br>' +
                    '<span style="color:var(--dh-text-muted);">Header: X-API-Key: ' + escapeHtml(apiKey) + '</span>' +
                '</div>' +
            '</div>' +
            '<div class="form-group">' +
                '<label><i data-lucide="code"></i> 调用示例</label>' +
                '<div style="font-family:var(--font-mono);font-size:12px;background:var(--dh-surface-muted);padding:12px;border-radius:var(--dh-radius-sm);border:1px solid var(--dh-border);line-height:1.6;white-space:pre-wrap;word-break:break-all;">' +
                    '<strong># 解析链接（返回下载地址）</strong>\n' +
                    'curl -X POST ' + escapeHtml(endpoints.resolve || '') + ' \\\n' +
                    '  -H "X-API-Key: ' + escapeHtml(apiKey) + '" \\\n' +
                    '  -H "Content-Type: application/json" \\\n' +
                    '  -d \'{"links": "https://www.douyin.com/video/xxx"}\'\n\n' +
                    '<strong># 解析+本地下载</strong>\n' +
                    'curl -X POST ' + escapeHtml(endpoints.download || '') + ' \\\n' +
                    '  -H "X-API-Key: ' + escapeHtml(apiKey) + '" \\\n' +
                    '  -H "Content-Type: application/json" \\\n' +
                    '  -d \'{"links": "https://www.douyin.com/video/xxx", "target_dir": "D:\\\\Downloads"}\'' +
                '</div>' +
            '</div>' +
            '<div style="padding:10px;background:var(--dh-surface-muted);border-radius:var(--dh-radius-sm);border-left:3px solid var(--dh-accent);font-size:13px;color:var(--dh-text-muted);">' +
                '<i data-lucide="info" style="width:14px;height:14px;vertical-align:middle;margin-right:4px;"></i> ' +
                '供外部设备或脚本调用，网页端无需使用。' +
            '</div>' +
            '</div>';
        if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    } catch (e) {
        contentEl.innerHTML = '<div class="text-muted" style="padding:12px;">加载 API 信息失败: ' + escapeHtml(e.message || '') + '</div>';
    }
}

function copyText(text) {
    if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function() { showToast('已复制', 'success'); });
    }
}

// ============================================================
// ====== 三模式动作（解析 / 一键下载 / API） ======
// ============================================================
function onResolve() { resolveSingleWorks(); }
function onQuick() { quickDownload(); }
function onApi() { switchToApiMode(); }

// ============================================================
// ====== 链接平台识别（前端预检，最终以后端解析为准） ======
// ============================================================
function detectPlatform(url) {
    var u = String(url || '').trim().toLowerCase();
    if (/(douyin\.com|iesdouyin\.com)/.test(u)) return { p: 'douyin', name: '抖音', cls: 'dy' };
    if (/tiktok\.com/.test(u)) return { p: 'tiktok', name: 'TikTok', cls: 'tt' };
    if (/(xiaohongshu\.com|xhslink\.com)/.test(u)) return { p: 'xhs', name: '小红书', cls: 'xhs' };
    return null;
}

// 全局 URL 提取正则（与后端 single_work.URL 保持一致）
var URL_RE = /https?:\/\/[^\s"'<>]+|(?:v\.douyin\.com|t-a\.cn|iesdouyin\.com|vm\.tiktok\.com|vt\.tiktok\.com)\/[^\s"'<>]+/g;

function parseLinkStats() {
    var raw = String(el('links-input').value || '');
    var matches = raw.match(URL_RE) || [];
    var counts = { douyin: 0, tiktok: 0, xhs: 0, bad: 0 };
    var seen = {};
    var total = 0;
    var firstBadUrl = '';
    matches.forEach(function(url) {
        var key = url.split('?')[0].replace(/\/$/, '');
        if (seen[key]) return; // 去重
        seen[key] = true;
        total++;
        var d = detectPlatform(url);
        if (d) { counts[d.p]++; }
        else { counts.bad++; if (!firstBadUrl) firstBadUrl = url; }
    });
    return { counts: counts, total: total, firstBadUrl: firstBadUrl, rawCount: matches.length };
}

// 链接识别：汇总统计条（数量 + 平台分布 + 匹配状态），不再逐行展示 URL
function renderLinkLines() {
    var s = parseLinkStats();
    var c = s.counts;
    var matched = s.total - c.bad;
    var dupInfo = (s.rawCount > s.total) ? '（已去重 ' + (s.rawCount - s.total) + '）' : '';
    var html = '<div class="link-summary' + (c.bad ? ' has-bad' : '') + '">';
    html += '<span class="ls-count"><i data-lucide="link-2"></i> ' + s.total + ' 个链接' + dupInfo + ' · 匹配 ' + matched + '/' + s.total + '</span>';
    html += '<span class="ls-badges">';
    if (c.douyin) html += '<span class="ls-pill dy">抖音 ' + c.douyin + '</span>';
    if (c.tiktok) html += '<span class="ls-pill tt">TikTok ' + c.tiktok + '</span>';
    if (c.xhs) html += '<span class="ls-pill xhs">小红书 ' + c.xhs + '</span>';
    if (c.bad) html += '<span class="ls-pill bad" title="点击定位到无法识别的链接" onclick="focusBadLink()"><i data-lucide="x"></i>无法识别 ' + c.bad + '</span>';
    html += '</span></div>';
    var container = el('link-lines');
    if (container) { container.innerHTML = html; }
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
}

// 点击「无法识别」→ 聚焦输入框
function focusBadLink() {
    var ta = el('links-input');
    if (!ta) return;
    ta.focus();
    var ls = document.querySelector('.link-summary');
    if (ls) { ls.classList.add('flash'); setTimeout(function() { ls.classList.remove('flash'); }, 800); }
}

// ============================================================
// ====== 下载设置折叠 ======
// ============================================================
function toggleSettings() {
    var block = el('settings-panel');
    if (!block) return;
    var open = block.style.display !== 'none';
    block.style.display = open ? 'none' : 'block';
    block.classList.toggle('open', !open);
    var btn = el('settings-btn');
    if (btn) btn.classList.toggle('active', !open);
}

function ensureSettingsOpen() {
    var block = el('settings-panel');
    if (block) { block.style.display = 'block'; block.classList.add('open'); }
    var btn = el('settings-btn');
    if (btn) btn.classList.add('active');
}

// ===== 低频资产（音乐/封面）折叠组 =====
function toggleAuxAssets(workId) {
    var group = document.querySelector('.asset-aux-group[data-work-id="' + workId + '"]');
    if (!group) return;
    var open = group.style.display !== 'none';
    group.style.display = open ? 'none' : 'block';
    var container = document.querySelector('.work-assets[data-work-id="' + workId + '"]');
    if (container) container.classList.toggle('aux-open', !open);
    var toggle = document.querySelector('.asset-aux-toggle[data-work-id="' + workId + '"]');
    if (toggle) toggle.classList.toggle('open', !open);
}

// 折叠 / 展开整个作品卡片的资产区（作品较多时方便快速收起）
function toggleWorkCollapsed(workId) {
    var card = document.querySelector('.work-card[data-work-id="' + cssEscape(workId) + '"]');
    if (!card) return;
    card.classList.toggle('collapsed');
}

function updateSettingsSummary() {
    var summary = el('settings-summary');
    if (summary) summary.textContent = el('single-target-dir').value || '未设置目录';
}

// ============================================================
// ====== 跨卡片选中聚合 + 浮动操作条 ======
// ============================================================
function updateSelection() {
    var checks = document.querySelectorAll('.asset-checkbox:checked');
    var count = checks.length;
    el('fb-count-text').textContent = '已选 ' + count + ' 个资产';
    var per = {};
    checks.forEach(function(c) { var w = c.dataset.workId; per[w] = (per[w] || 0) + 1; });
    var keys = Object.keys(per);
    el('fb-sub').textContent = keys.length ? ('来自 ' + keys.length + ' 个作品') : '';
    // 底部常驻下载栏：主按钮随选中态切换 + 清空按钮显隐
    var dlBtn = el('fb-download-btn'), dlLabel = el('fb-download-label'), clearBtn = el('fb-clear-btn');
    if (dlBtn && dlLabel) {
        if (count > 0) {
            dlBtn.classList.add('btn-primary'); dlBtn.classList.remove('btn-secondary');
            dlLabel.textContent = '下载 ' + count + ' 项';
        } else {
            dlBtn.classList.remove('btn-primary'); dlBtn.classList.add('btn-secondary');
            dlLabel.textContent = '选择资产以下载';
        }
    }
    if (clearBtn) clearBtn.style.display = count > 0 ? '' : 'none';
    // 每卡片工具栏选中计数（若存在）
    document.querySelectorAll('.toolbar-count-selected').forEach(function(span) {
        var n = checks.length ? Array.from(checks).filter(function(c) { return c.dataset.workId === span.dataset.workId; }).length : 0;
        span.textContent = n ? ' · 已选 ' + n : '';
    });
}

function clearAllSelection() {
    document.querySelectorAll('.asset-checkbox').forEach(function(b) {
        b.checked = false;
        var row = b.closest('.work-asset-row');
        if (row) row.classList.remove('selected');
    });
    updateSelection();
}

function downloadAllSelected() {
    var per = {};
    document.querySelectorAll('.asset-checkbox:checked').forEach(function(b) {
        var w = b.dataset.workId;
        if (!per[w]) per[w] = [];
        per[w].push(parseInt(b.dataset.assetIndex));
    });
    var keys = Object.keys(per);
    if (!keys.length) { showToast('请先勾选要下载的资产', 'error'); return; }
    var total = keys.reduce(function(n, k) { return n + per[k].length; }, 0);
    var els = startDownloadProgress(total, total);
    batchEls = els;
    (async function() {
        var doneWorks = 0;
        for (var i = 0; i < keys.length; i++) {
            var work = findWorkById(keys[i]);
            if (work) {
                await doDownloadSelectedAssets(keys[i], work.share_url, per[keys[i]], els);
            }
            doneWorks++;
            els.text.textContent = '正在下载 (' + doneWorks + '/' + keys.length + ' 个作品)';
        }
        // 收尾：全部完成
        var failed = parseInt(els.failed.textContent) || 0;
        var ok = failed === 0;
        els.status.className = 'workflow-status ' + (ok ? 'success' : 'failed');
        els.status.textContent = ok ? '已完成' : '部分失败';
        renderSseSegments(els.segments, 3, 3, !ok);
        batchEls = null;
        clearAllSelection();
    })();
}

// ============================================================
// ====== 浮层（API 提示 / 下载方式选择） ======
// ============================================================
var pendingDlAction = null;

function showPop(id) { var node = document.getElementById(id); if (node) node.style.display = 'flex'; }
function closePop(id) { var node = document.getElementById(id); if (node) node.style.display = 'none'; }

function openDlChoice(onConfirm, count) {
    var n = count !== undefined ? count : document.querySelectorAll('.asset-checkbox:checked').length;
    if (!n && !onConfirm) { showToast('请先勾选要下载的资产', 'error'); return; }
    pendingDlAction = onConfirm || null;
    if (el('dl-choice-count')) el('dl-choice-count').textContent = n;
    var dir = el('single-target-dir').value;
    if (el('dl-local-sub')) el('dl-local-sub').textContent = '保存到本机目录' + (dir ? ': ' + dir : '');
    document.querySelectorAll('.dl-choice .choice').forEach(function(c) {
        c.classList.toggle('active', c.dataset.dl === currentDownloadMode);
    });
    showPop('dl-choice-overlay');
}

function chooseDl(mode) {
    setDownloadMode(mode);
    closePop('dl-choice-overlay');
    var fn = pendingDlAction;
    pendingDlAction = null;
    if (fn) fn();
}

// ============================================================
// ====== 问题5：资产网格视图切换 ======
// ============================================================
function toggleAssetView(workId) {
    var container = document.querySelector('.work-assets[data-work-id="' + workId + '"]');
    if (!container) return;
    var isGrid = container.classList.contains('grid-view');
    container.classList.toggle('grid-view', !isGrid);
    // 切换按钮图标
    var btn = document.querySelector('.view-toggle[data-work-id="' + workId + '"]');
    if (btn) {
        btn.querySelector('svg[data-lucide], i[data-lucide]')?.setAttribute('data-lucide', isGrid ? 'layout-grid' : 'list');
    }
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
}

// ============================================================
// ====== 问题3：下载历史增强 ======
// ============================================================
function safeParseJson(v) { try { return JSON.parse(v); } catch(e) { return null; } }

function truncateUrl(url, max) {
    max = max || 60;
    return String(url || '').length > max ? String(url).slice(0, max) + '...' : String(url);
}

function shortenPath(path) {
    if (!path) return '';
    var parts = String(path).split(/[\\/]/);
    if (parts.length <= 3) return path;
    return '.../' + parts.slice(-2).join('/');
}

function copyHistoryLink(link) {
    if (navigator.clipboard && link) {
        navigator.clipboard.writeText(link).then(function() { showToast('已复制链接', 'success'); });
    }
}

async function openHistoryDir(historyId) {
    try {
        var data = await apiCall('/api/collection/works/history/' + historyId + '/open-dir', 'POST');
        if (data.success) { showToast('已打开目录', 'success'); }
        else { showToast(data.message || '打开失败', 'error'); }
    } catch(e) { showToast(e.message || '打开失败', 'error'); }
}

// ============================================================
// ====== 问题6：录入分享表 ======
// ============================================================
function addToShareTable(workId) {
    var work = findWorkById(workId);
    if (!work) { showToast('未找到作品数据', 'error'); return; }
    var secUid = (work.author_info || {}).sec_uid || '';
    if (!secUid) { showToast('该作品未包含 sec_user_id，无法录入', 'error'); return; }
    var authorName = work.author || (work.author_info || {}).nickname || '';
    var platform = work.platform === 'tiktok' ? 'TikTok' : '抖音';
    var shareUrl = work.share_url || '';
    // 预填
    el('share-add-name').value = authorName;
    el('share-add-platform').value = platform;
    el('share-add-rating').value = '3';
    el('share-add-tags').value = '';
    el('share-add-sec-uid').value = secUid;
    el('share-add-source-link').value = shareUrl;
    el('share-add-note').value = '从单作品采集录入';
    // 显示弹窗
    showPop('share-add-overlay');
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
}

function closeShareAdd() { closePop('share-add-overlay'); }

async function confirmAddToShare() {
    var secUid = el('share-add-sec-uid').value.trim();
    if (!secUid) { showToast('缺少 sec_user_id', 'error'); return; }
    var btn = el('share-add-confirm');
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> 录入中...';
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    try {
        var tags = el('share-add-tags').value.split(',').map(function(t) { return t.trim(); }).filter(Boolean);
        var data = await apiCall('/api/collection/quick-add-share', 'POST', {
            sec_user_id: secUid,
            platform: el('share-add-platform').value,
            account_name: el('share-add-name').value.trim(),
            rating: parseInt(el('share-add-rating').value),
            tags: tags,
            source_link: el('share-add-source-link').value.trim(),
            note: el('share-add-note').value.trim(),
        });
        if (data.success) {
            showToast(data.message || '已录入分享表', 'success');
            closeShareAdd();
        } else {
            showToast(data.message || '录入失败', 'error');
        }
    } catch(e) {
        showToast(e.message || '录入失败', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="check"></i> 确认录入';
        if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    }
}