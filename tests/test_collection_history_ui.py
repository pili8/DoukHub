import json
import re
import subprocess
from pathlib import Path


def _function(source, name):
    match = re.search(
        rf"function {re.escape(name)}\(.*?\n\}}", source, re.DOTALL
    )
    assert match is not None, f"function not found: {name}"
    return match.group(0)


def test_history_batch_detail_renders_db_works_without_log():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    script = "\n".join(
        _function(source, name)
        for name in (
            "_bdStatusGroup",
            "_bdFilterItems",
            "_bdWorkRowsHtml",
            "renderBdItems",
            "loadMoreBdItems",
        )
    )
    program = f"""
const elements = {{}};
global.document = {{ getElementById(id) {{
    if (!elements[id]) elements[id] = {{ textContent: '', innerHTML: '' }};
    return elements[id];
}} }};
global.window = {{}};
global._bdData = {{
    items: Array.from({{length: 130}}, (_, i) => ({{ id: i, sec_user_id: 'sec-' + i, account_name: 'account-' + i, status: 'success', message: '' }})),
    works: {{ 'account-0': {{ video: ['work-one'], image: [], live: [], total: 1 }} }},
    works_db: [{{ sec_user_id: 'sec-0', account_name: 'account-0', title: 'work-one', kind: 'video', status: 'success', file_name: 'a.mp4', download_dir: 'D:/works' }}],
    works_source: 'db',
    log_exists: false
}};
global._bdFilter = {{ kw: '', st: '' }};
global._bdVisible = 0;
global._BD_CHUNK = 120;
global.escapeHtml = s => String(s == null ? '' : s);
global.escapeJs = escapeHtml;
global.formatBatchStatus = s => s;
global.formatWorkStatus = s => s;
global.formatDateTime = s => s;
global.openAccountWorks = () => {{}};
global.AW_open = () => {{}};
{script}
renderBdItems();
const first = elements['bd-list-body'].innerHTML;
const firstCount = elements['bd-count'].textContent;
loadMoreBdItems();
const second = elements['bd-list-body'].innerHTML;
console.log(JSON.stringify({{
    count: (firstCount.match(/\\d+/g) || []).map(Number),
    firstHasWorks: first.includes('data-bd-works="0"') && first.includes('work-one'),
    firstShowsChunk: first.includes('load-more-row') && !first.includes('sec-129'),
    secondShowsAll: second.includes('sec-129') && !second.includes('load-more-row')
}}));
"""
    result = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rendered = json.loads(result.stdout)
    assert rendered == {
        "count": [120, 130],
        "firstHasWorks": True,
        "firstShowsChunk": True,
        "secondShowsAll": True,
    }
