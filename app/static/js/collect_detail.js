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
function showProgressPanel(title) {
    var d = document.getElementById('single-progress');
    d.style.display = 'block';
    d.innerHTML =
        '<div style="border:1px solid var(--dh-border);border-radius:var(--dh-radius);padding:14px;background:var(--dh-surface);">' +
        '<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px;">' +
            '<div class="workflow-title"><i data-lucide="activity"></i><span>' + title + '</span></div>' +
            '<span class="workflow-status running" id="sse-status">准备中...</span>' +
        '</div>' +
        '<div style="height:6px;background:var(--dh-surface-muted);border-radius:3px;overflow:hidden;">' +
            '<div id="sse-bar" style="height:100%;width:0%;background:var(--dh-accent);border-radius:3px;transition:width .3s;"></div>' +
        '</div>' +
        '<div class="workflow-progress-meta"><span id="sse-text">等待开始...</span><span id="sse-count"></span></div>' +
        '<div class="workflow-metrics" style="margin-top:10px;">' +
            '<div class="workflow-metric" style="border-left-color:var(--dh-accent);"><span class="metric-value" id="sse-total" style="color:var(--dh-accent);">0</span><span class="metric-label">总数</span></div>' +
            '<div class="workflow-metric" style="border-left-color:var(--md-success);"><span class="metric-value" id="sse-success" style="color:var(--md-success);">0</span><span class="metric-label">成功</span></div>' +
            '<div class="workflow-metric" style="border-left-color:var(--md-error);"><span class="metric-value" id="sse-failed" style="color:var(--md-error);">0</span><span class="metric-label">失败</span></div>' +
        '</div>' +
        '<div class="workflow-log" id="sse-log" style="margin-top:10px;max-height:200px;"></div>' +
        '</div>';
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    return { status: el('sse-status'), bar: el('sse-bar'), text: el('sse-text'), count: el('sse-count'), total: el('sse-total'), success: el('sse-success'), failed: el('sse-failed'), log: el('sse-log') };
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
                addSseLog(els.log, data.message, 'info');
            } else if (data.type === 'progress') {
                var pct = data.total > 0 ? Math.round((data.index - 1) / data.total * 100) : 0;
                els.bar.style.width = pct + '%'; els.text.textContent = data.message; els.count.textContent = pct + '%';
                var phase = data.phase || '';
                var isFail = phase.indexOf('failed') >= 0, isDone = phase.indexOf('done') >= 0;
                els.status.className = 'workflow-status ' + (isFail ? 'failed' : (isDone ? 'success' : 'running'));
                els.status.textContent = isFail ? '失败' : (isDone ? '成功' : '执行中');
                addSseLog(els.log, data.message, isFail ? 'error' : (isDone ? 'ok' : 'info'));
                if (data.total && (phase === 'download_done' || phase === 'done')) els.bar.style.width = Math.round(data.index / data.total * 100) + '%';
                if (data.success_count !== undefined) els.success.textContent = data.success_count;
                if (data.failed_count !== undefined) els.failed.textContent = data.failed_count;
                if (phase === 'stage') showInlineStatus(data.message);
                if (onProgress) onProgress(data);
            } else if (data.type === 'complete') {
                els.bar.style.width = '100%'; els.bar.style.background = data.success ? 'var(--md-success)' : 'var(--md-error)';
                els.status.className = 'workflow-status ' + (data.success ? 'success' : 'failed');
                els.status.textContent = data.success ? '已完成' : '失败';
                els.text.textContent = data.message; els.count.textContent = '100%';
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

// ====== Download mode tabs ======
function setDownloadMode(mode) {
    currentDownloadMode = mode;
    document.querySelectorAll('.dl-mode-tab').forEach(function(btn) {
        btn.classList.toggle('active', btn.dataset.dlMode === mode);
    });
    var settingsPanel = el('settings-panel');
    if (settingsPanel) settingsPanel.style.display = 'block';
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
    event.preventDefault();
    var linksText = String(new FormData(event.target).get('links') || '');
    if (!linksText.trim()) { showToast('请先粘贴作品链接', 'error'); return; }
    var resolveMode = (el('resolve-mode-select') || {}).value || 'auto';
    var btnR = el('detail-resolve'), btnQ = el('quick-download-btn');
    btnR.disabled = true; btnQ.disabled = true;
    btnR.innerHTML = '<i data-lucide="loader-circle"></i> 解析中...';
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    showInlineStatus('准备中...');
    var els = showProgressPanel('解析进度');
    el('single-work-list').innerHTML = ''; singleWorkState.works = [];
    try {
        var resp = await fetch('/api/collection/works/resolve-stream', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({links: linksText, resolve_mode: resolveMode}) });
        await consumeSse(resp, els, function(data) {
            if (data.phase === 'done' && data.work) {
                singleWorkState.works.push(data.work);
                appendWorkCard(data.work);
                el('settings-panel').style.display = 'block';
                if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
            }
        }, function(data) {
            if (data.works && data.works.length) {
                resolvedSingleLinks = data.works.map(function(w) { return w.share_url; }).filter(Boolean);
                singleWorkState.works = data.works; renderSingleWorks(data.works);
                el('settings-panel').style.display = 'block';
            }
        });
    } catch (e) {
        els.status.textContent = '请求失败: ' + e.message; els.status.style.color = 'var(--danger)';
        addSseLog(els.log, '请求失败: ' + e.message, 'error'); showToast(e.message || '解析失败', 'error');
    } finally {
        btnR.disabled = false; btnQ.disabled = false;
        btnR.innerHTML = '<i data-lucide="search"></i> 解析';
        if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    }
}

// ====== Quick Download (fast, no mode selection, local only) ======
async function quickDownload() {
    var linksText = String(el('links-input').value || '');
    if (!linksText.trim()) { showToast('请先粘贴作品链接', 'error'); return; }
    var targetDir = el('single-target-dir').value || '';
    if (!targetDir) { showToast('请先设置保存目录', 'error'); el('settings-panel').style.display = 'block'; return; }
    var template = el('single-template-input').value || '{create_time} {author} {title}';
    var incMusic = el('opt-music').checked, incSC = el('opt-static-cover').checked, incDC = el('opt-dynamic-cover').checked;
    var btnR = el('detail-resolve'), btnQ = el('quick-download-btn');
    btnR.disabled = true; btnQ.disabled = true;
    btnQ.innerHTML = '<i data-lucide="loader-circle"></i> 下载中...';
    if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    showInlineStatus('快速下载中...');
    var els = showProgressPanel('下载进度');
    el('single-work-list').innerHTML = ''; singleWorkState.works = [];
    try {
        var resp = await fetch('/api/collection/works/download-stream', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ links: linksText, target_dir: targetDir, filename_template: template, include_music: incMusic, include_static_cover: incSC, include_dynamic_cover: incDC }) });
        await consumeSse(resp, els, function(data) {
            if (data.phase === 'resolve_done' && data.work) { singleWorkState.works.push(data.work); appendWorkCard(data.work); el('settings-panel').style.display = 'block'; }
            if (data.phase === 'download_done') markWorkDownloaded(data.title, data.files);
            if (data.phase === 'download_failed') markWorkFailed(data.message);
        }, async function(data) { await saveRecentSingleDir(targetDir); await loadSingleWorkHistory(); });
    } catch (e) {
        els.status.textContent = '请求失败: ' + e.message; els.status.style.color = 'var(--danger)';
        addSseLog(els.log, '请求失败: ' + e.message, 'error'); showToast(e.message || '下载失败', 'error');
    } finally {
        btnR.disabled = false; btnQ.disabled = false;
        btnQ.innerHTML = '<i data-lucide="zap"></i> 一键下载';
        if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
    }
}

function findWorkById(workId) {
    return singleWorkState.works.find(function(w) { return String(w.id) === String(workId); });
}

function findWorkByShareUrl(shareUrl) {
    return singleWorkState.works.find(function(w) { return w.share_url === shareUrl; });
}

// ====== Single asset download ======
async function downloadSingleAsset(link, assetIndex) {
    var dlMode = getDownloadMode();
    var work = findWorkByShareUrl(link);
    var asset = work ? (work.assets || []).find(function(a) { return a.index === assetIndex; }) : null;
    if (!asset) { showToast('未找到资产', 'error'); return; }
    if (dlMode === 'browser') {
        var fname = asset.kind;
        try { fname = (work.author || '') + ' ' + (work.title || asset.kind); } catch(e) {}
        proxyDownloadAsset(asset.url, fname);
        showToast('已开始浏览器下载: ' + assetKindLabel(asset.kind), 'success');
        return;
    }
    var targetDir = el('single-target-dir').value || '';
    if (!targetDir) { showToast('请先选择保存目录', 'error'); el('settings-panel').style.display = 'block'; return; }
    var template = el('single-template-input').value || '{create_time} {author} {title}';
    try {
        var data = await apiCall('/api/collection/works/download', 'POST', { links: link, target_dir: targetDir, filename_template: template, asset_indexes: [assetIndex], work: work });
        showToast(data.success ? '下载成功' : '下载失败', data.success ? 'success' : 'error');
        await loadSingleWorkHistory();
    } catch (error) { showToast(error.message || '下载失败', 'error'); }
}

// ====== Asset selection & batch download ======
function onAssetCheckboxChange(workId, assetIndex, checked) {
    var row = document.querySelector('.work-assets[data-work-id="' + workId + '"] .work-asset-row[data-asset-index="' + assetIndex + '"]');
    if (row) row.classList.toggle('selected', checked);
}

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
        showToast('已开始浏览器下载 ' + indexes.length + ' 个资产', 'success');
        return;
    }
    var targetDir = el('single-target-dir').value || '';
    if (!targetDir) { showToast('请先选择保存目录', 'error'); el('settings-panel').style.display = 'block'; return; }
    var template = el('single-template-input').value || '{create_time} {author} {title}';
    try {
        var data = await apiCall('/api/collection/works/download', 'POST', {
            links: shareUrl, target_dir: targetDir, filename_template: template, asset_indexes: indexes, work: work
        });
        showToast(data.success ? '下载 ' + indexes.length + ' 个资产成功' : '下载失败', data.success ? 'success' : 'error');
        await loadSingleWorkHistory();
    } catch (error) { showToast(error.message || '下载失败', 'error'); }
}

// ====== Work card rendering ======
function buildWorkCardHtml(work) {
    var assets = work.assets || [];
    var stats = work.stats || {};
    var music = work.music || {};
    var media = work.media || {};
    var cover = work.static_cover || '';
    var authorInfo = work.author_info || {};
    var workType = work.type || '';

    var primaryAssets = assets.filter(function(a) { return ['video','image','live_photo'].indexOf(a.kind) >= 0; });

    var html = '<div class="work-card" data-work-id="' + escapeHtml(work.id) + '">';

    // Left: cover (click to enlarge)
    if (cover) {
        html += '<div class="work-cover-wrap" onclick="openLightbox(\'' + cover.replace(/'/g, "\\'") + '\')" style="cursor:zoom-in;">';
        html += '<img src="' + escapeHtml(cover) + '" loading="lazy" alt="封面">';
        html += '<div class="work-cover-badge">';
        html += typeBadge(workType);
        html += '</div>';
        html += '</div>';
    } else {
        html += '<div class="work-cover-wrap" style="display:flex;align-items:center;justify-content:center;">';
        html += '<i data-lucide="' + (workType === '视频' ? 'video' : 'images') + '" style="width:32px;height:32px;color:var(--dh-text-muted);"></i>';
        html += '<div class="work-cover-badge">' + typeBadge(workType) + '</div>';
        html += '</div>';
    }

    // Right: info
    html += '<div class="work-info">';

    // Description
    var desc = work.desc || work.title || '';
    if (desc && desc !== work.id) {
        html += '<div class="work-desc">' + escapeHtml(desc) + '</div>';
    }

    // Badges
    html += '<div class="work-badges">';
    if (!cover) html += typeBadge(workType);
    html += platformBadge(work.platform);
    if (primaryAssets.length > 1) html += '<span class="workflow-status pending">' + primaryAssets.length + ' 个' + (workType === '视频' ? '视频' : '图片') + '</span>';
    var mediaParts = [];
    if (media.duration && media.duration !== '00:00:00') mediaParts.push('<i data-lucide="clock" style="width:13px;height:13px;"></i> ' + escapeHtml(media.duration));
    if (media.width > 0 && media.height > 0) mediaParts.push('<i data-lucide="ratio" style="width:13px;height:13px;"></i> ' + media.width + '\u00d7' + media.height);
    if (mediaParts.length) html += '<span class="workflow-status pending">' + mediaParts.join(' \u00b7 ') + '</span>';
    html += '</div>';

    // Stats
    html += '<div class="work-stats">';
    html += '<span class="work-stat"><i data-lucide="heart"></i><span class="stat-num">' + formatCount(stats.digg_count) + '</span> 赞</span>';
    html += '<span class="work-stat"><i data-lucide="message-circle"></i><span class="stat-num">' + formatCount(stats.comment_count) + '</span> 评</span>';
    html += '<span class="work-stat"><i data-lucide="star"></i><span class="stat-num">' + formatCount(stats.collect_count) + '</span> 藏</span>';
    html += '<span class="work-stat"><i data-lucide="share-2"></i><span class="stat-num">' + formatCount(stats.share_count) + '</span> 转</span>';
    html += '</div>';

    // Meta
    html += '<div class="work-meta">';
    html += '<span class="work-meta-item"><i data-lucide="user"></i> ' + escapeHtml(work.author || authorInfo.nickname || '未知') + '</span>';
    if (work.create_time) html += '<span class="work-meta-item"><i data-lucide="calendar"></i> ' + escapeHtml(work.create_time) + '</span>';
    if (music.title) {
        html += '<span class="work-meta-item"><i data-lucide="music"></i> ' + escapeHtml(music.title);
        if (music.author) html += ' - ' + escapeHtml(music.author);
        html += '</span>';
    }
    html += '</div>';

    // Tags
    var tags = (work.hashtags || []).concat(work.video_tags || []);
    if (tags.length) {
        html += '<div class="work-tags">';
        tags.forEach(function(tag) {
            html += '<span class="work-tag">#' + escapeHtml(tag) + '</span>';
        });
        html += '</div>';
    }

    // Assets list with toolbar
    if (assets.length) {
        var kindGroups = {};
        assets.forEach(function(a) {
            if (!kindGroups[a.kind]) kindGroups[a.kind] = [];
            kindGroups[a.kind].push(a.index);
        });
        var kindKeys = Object.keys(kindGroups);

        html += '<div class="work-asset-toolbar">';
        html += '<span class="toolbar-count">' + assets.length + ' 个资产</span>';
        html += '<button type="button" class="toolbar-btn" onclick="toggleAllAssets(this,\'' + work.id + '\')"><i data-lucide="check-square"></i> 全选</button>';
        kindKeys.forEach(function(k) {
            if (kindKeys.length > 1 || kindGroups[k].length > 1) {
                html += '<button type="button" class="toolbar-btn" onclick="selectAssetsByKind(\'' + work.id + '\',\'' + k + '\')"><i data-lucide="' + assetKindIcon(k) + '"></i> ' + escapeHtml(assetKindLabel(k)) + '(' + kindGroups[k].length + ')</button>';
            }
        });
        html += '<button type="button" class="toolbar-btn primary" onclick="downloadSelectedAssets(\'' + work.id + '\',\'' + work.share_url.replace(/'/g, "\\'") + '\')"><i data-lucide="download"></i> 下载选中</button>';
        html += '</div>';

        // Asset rows with thumbnails (clickable to enlarge)
        html += '<div class="work-assets" data-work-id="' + escapeHtml(work.id) + '">';
        assets.forEach(function(a) {
            var thumb = a.cover_url || (a.kind === 'static_cover' || a.kind === 'dynamic_cover' ? a.url : '');
            html += '<div class="work-asset-row" data-asset-index="' + a.index + '">';
            html += '<input type="checkbox" class="asset-checkbox" data-work-id="' + escapeHtml(work.id) + '" data-asset-index="' + a.index + '" data-asset-kind="' + escapeHtml(a.kind) + '" onchange="onAssetCheckboxChange(\'' + work.id + '\',' + a.index + ', this.checked)">';
            // Thumbnail (clickable for lightbox)
            if (thumb) {
                html += '<div class="asset-thumb" onclick="openLightbox(\'' + thumb.replace(/'/g, "\\'") + '\')" style="cursor:zoom-in;"><img src="' + escapeHtml(thumb) + '" loading="lazy" alt="预览"></div>';
            } else {
                html += '<div class="asset-thumb"><i data-lucide="' + assetKindIcon(a.kind) + '"></i></div>';
            }
            html += '<span class="asset-label">' + escapeHtml(assetKindLabel(a.kind)) + (assets.length > 1 ? ' ' + a.index : '') + '</span>';
            html += '<span class="asset-url" title="' + escapeHtml(a.url) + '">' + escapeHtml(a.url) + '</span>';
            html += '<div class="asset-actions">';
            html += '<button type="button" title="复制链接" onclick="copyAssetUrl(\'' + a.url.replace(/'/g, "\\'") + '\')"><i data-lucide="copy"></i></button>';
            html += '<a href="' + escapeHtml(a.url) + '" target="_blank" title="打开"><i data-lucide="external-link"></i></a>';
            html += '<button type="button" title="下载此项" onclick="downloadSingleAsset(\'' + work.share_url.replace(/'/g, "\\'") + '\',' + a.index + ')"><i data-lucide="download"></i></button>';
            html += '</div>';
            html += '</div>';
        });
        html += '</div>';
    }

    // Download status area
    html += '<div class="work-download-status-area" data-work-title="' + escapeHtml(work.title || work.id) + '"></div>';

    html += '</div>'; // work-info
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
        var isOk = row.status === 'success';
        var sub = isOk
            ? (files.length + ' 个文件 · ' + escapeHtml(row.work_type || ''))
            : escapeHtml(row.error || '未知错误');
        var actions = !isOk
            ? ' <button type="button" class="btn btn-secondary" style="height:28px;padding:0 10px;font-size:12px;flex-shrink:0;" onclick="retrySingleWorkHistory(' + row.id + ')"><i data-lucide="refresh-cw" style="width:12px;height:12px;"></i> 重试</button>'
            : '';
        return '<div class="history-card">' +
            '<div class="hist-icon ' + (isOk ? 'success' : 'failed') + '"><i data-lucide="' + (isOk ? 'check' : 'x') + '"></i></div>' +
            '<div class="hist-body">' +
                '<div class="hist-title">' + escapeHtml(row.title || row.work_id) + '</div>' +
                '<div class="hist-sub">' + sub + '</div>' +
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