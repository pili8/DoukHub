import json
import re
import subprocess
from pathlib import Path

from app.core.syncer_v2 import Syncer


def _function(source, name):
    match = re.search(
        rf"(?:async\s+)?function {re.escape(name)}\(.*?\n    \}}",
        source,
        re.DOTALL,
    )
    assert match is not None, f"function not found: {name}"
    return match.group(0)


# 预览解析用到的前端函数，必须与后端 syncer_v2 的解析规则保持一致
PREVIEW_FUNCTIONS = (
    "mapTag",
    "parseGradeTags",
    "mergeGrade",
    "parseSimpleFormat",
    "parseJsonFormat",
)


def _preview_script(source):
    return "\n".join(_function(source, name) for name in PREVIEW_FUNCTIONS)


def test_import_preview_parses_real_world_mixed_formats():
    source = Path("app/templates/sync/import.html").read_text(encoding="utf-8")
    script = _preview_script(source)
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
    script = _preview_script(source) + "\n" + _function(source, "parseImport")
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


def test_preview_grade_rules_match_backend():
    """预览解析的等级/标签必须与后端 parse_grade_tags 完全一致。

    规则：@ 前的数字是等级，同时充当标签分隔符（"2个"→等级2+标签"个"）。
    """
    source = Path("app/templates/sync/import.html").read_text(encoding="utf-8")
    script = _preview_script(source)
    grades = ["2个", "个2", "COS2", "酒吧3多", "个3，多", "2", "多", ""]
    program = f"""
global.TAGS_MAPPING = {{'个': '个人'}};
var parsedData = [];
{script}
var out = [];
{json.dumps(grades, ensure_ascii=False)}.forEach(function(g) {{
    parseSimpleFormat(g + '@87X9S198AQY');
    var item = parsedData[0] || {{rating: 1, tags: []}};
    out.push({{grade: g, rating: item.rating, tags: item.tags}});
}});
console.log(JSON.stringify(out));
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

    mapping = {"个": "个人"}
    expected = []
    for grade in grades:
        level, tags = Syncer.parse_grade_tags(grade)
        expected.append(
            {"grade": grade, "rating": level, "tags": [mapping.get(t, t) for t in tags]}
        )
    assert rendered == expected
