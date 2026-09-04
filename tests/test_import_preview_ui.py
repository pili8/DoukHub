import json
import re
import subprocess
from pathlib import Path


def _function(source, name):
    match = re.search(
        rf"(?:async\s+)?function {re.escape(name)}\(.*?\n    \}}",
        source,
        re.DOTALL,
    )
    assert match is not None, f"function not found: {name}"
    return match.group(0)


def test_import_preview_parses_real_world_mixed_formats():
    source = Path("app/templates/sync/import.html").read_text(encoding="utf-8")
    script = "\n".join(
        _function(source, name)
        for name in ("mapTag", "parseSimpleFormat", "parseJsonFormat")
    )
    simple_text = """
个，图@ihNoyCMM
个，2\\@ihYfCafE
分享，图，2\\@ihYfWvum
个，商业，2\\@ih2fYvqA
个2，多@if1Mrqtx
COS2\\@if1uhbyh
酒吧2\\@ifJJqJQx
分享2\\@ifJEuXDU
"""
    json_text = """
{"ID号" :"","作品" :"","地址" :"Wfdc1A6ewbg","时间" :"20260621231530","用户" :"","等级" :"个3","粉丝" :""}
{"ID号" :"","作品" :"","地址" :"seX062YZFK0","时间" :"20260621232010","用户" :"","等级" :"个3","粉丝" :""}
{"ID号" :"","作品" :"","地址" :"vQ2mKm6YAPo","时间" :"20260622104047","用户" :"","等级" :"自拍3","粉丝" :""}
{"ID号" :"41089775107","作品" :"作品 55","地址" :"VtaXSs2w1P0","时间" :"20250917102014","用户" :"刘鑫泽他爹开的A7L","等级" :"个3","粉丝" :"1.5万"}
{"ID号" :"WMWMWMYYY","作品" :"作品 383","地址" :"1SVatf0jI-s","时间" :"20250917104207","用户" :"一筒","等级" :"个3，多","粉丝" :"24.2万"}
"""
    program = f"""
global.TAGS_MAPPING = {{'个': '个人'}};
var parsedData = [];
{script}
parseSimpleFormat({json.dumps(simple_text, ensure_ascii=False)});
var simple = parsedData.slice();
parseJsonFormat({json.dumps(json_text, ensure_ascii=False)});
var escaped = simple.find(x => x.link === 'ihYfCafE') || {{}};
var named = parsedData.find(x => x.link === 'VtaXSs2w1P0') || {{}};
var tagged = parsedData.find(x => x.link === '1SVatf0jI-s') || {{}};
console.log(JSON.stringify({{
    simpleCount: simple.length,
    simpleEscapedRating: escaped.rating,
    simpleEscapedTags: escaped.tags,
    jsonCount: parsedData.length,
    jsonName: named.name,
    jsonTags: tagged.tags
}}));
"""
    result = subprocess.run(
        ["node", "-e", program],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert rendered == {
        "simpleCount": 8,
        "simpleEscapedRating": 2,
        "simpleEscapedTags": ["个人"],
        "jsonCount": 5,
        "jsonName": "刘鑫泽他爹开的A7L",
        "jsonTags": ["个人", "多"],
    }


def test_import_preview_merges_simple_and_json_in_one_paste():
    source = Path("app/templates/sync/import.html").read_text(encoding="utf-8")
    script = "\n".join(
        _function(source, name)
        for name in ("mapTag", "parseSimpleFormat", "parseJsonFormat", "parseImport")
    )
    text = """
个，图@ihNoyCMM
个，2\\@ihYfCafE
分享，图，2\\@ihYfWvum
个，商业，2\\@ih2fYvqA
个2，多@if1Mrqtx
COS2\\@if1uhbyh
酒吧2\\@ifJJqJQx
分享2\\@ifJEuXDU

{"ID号" :"","作品" :"","地址" :"Wfdc1A6ewbg","时间" :"20260621231530","用户" :"","等级" :"个3","粉丝" :""}
{"ID号" :"","作品" :"","地址" :"seX062YZFK0","时间" :"20260621232010","用户" :"","等级" :"个3","粉丝" :""}
{"ID号" :"","作品" :"","地址" :"vQ2mKm6YAPo","时间" :"20260622104047","用户" :"","等级" :"自拍3","粉丝" :""}
{"ID号" :"41089775107","作品" :"作品 55","地址" :"VtaXSs2w1P0","时间" :"20250917102014","用户" :"刘鑫泽他爹开的A7L","等级" :"个3","粉丝" :"1.5万"}
{"ID号" :"WMWMWMYYY","作品" :"作品 383","地址" :"1SVatf0jI-s","时间" :"20250917104207","用户" :"一筒","等级" :"个3，多","粉丝" :"24.2万"}
"""
    program = f"""
global.TAGS_MAPPING = {{'个': '个人'}};
var parsedData = [];
{script}
async function loadTagsMapping() {{}}
function normalizePreviewLink(link) {{ return String(link || '').trim(); }}
function escapeHtml(value) {{ return String(value == null ? '' : value); }}
function isMappedTag() {{ return true; }}
function showToast() {{}}
var elements = {{
    'import-text': {{value: {json.dumps(text, ensure_ascii=False)}}},
    'preview-body': {{innerHTML: ''}},
    'import-preview': {{style: {{}}}},
    'import-status': {{innerHTML: ''}}
}};
global.document = {{getElementById: function(id) {{ return elements[id]; }} }};
(async function() {{
    await parseImport();
    console.log(JSON.stringify({{count: parsedData.length, status: elements['import-status'].innerHTML}}));
}})().catch(function(error) {{
    console.error(error.stack);
    process.exit(1);
}});
"""
    result = subprocess.run(
        ["node", "-e", program],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert rendered == {
        "count": 13,
        "status": "解析完成: <b>13</b> 条",
    }
