"""测试 API 端点是否正常响应"""
import urllib.request
import json
import sys

base_url = "http://127.0.0.1:2999"

# 测试1: 账号表 schema
try:
    print("=== 测试1: 账号表 schema ===")
    resp = urllib.request.urlopen(f"{base_url}/api/database/table/account_cache/schema", timeout=5)
    data = json.loads(resp.read())
    print(f"  Status: {resp.status}")
    print(f"  Fields count: {len(data.get('fields', []))}")
    for f in data.get("fields", [])[:5]:
        print(f"    {f['name']} ({f['type']})")
    print("  OK")
except Exception as e:
    print(f"  FAILED: {e}")

# 测试2: 账号表数据
try:
    print("\n=== 测试2: 账号表数据 ===")
    resp = urllib.request.urlopen(f"{base_url}/api/database/table/account_cache?limit=5&offset=0", timeout=5)
    data = json.loads(resp.read())
    print(f"  Status: {resp.status}")
    print(f"  Total: {data.get('total')}")
    print(f"  Records count: {len(data.get('records', []))}")
    if data.get("records"):
        print(f"  First record keys: {list(data['records'][0].keys())}")
        r = data['records'][0]
        print(f"  First record: record_id={r.get('record_id')}, platform={r.get('platform') or r.get('平台')}")
    print("  OK")
except Exception as e:
    print(f"  FAILED: {e}")

# 测试3: 采集表数据
try:
    print("\n=== 测试3: 采集表数据 ===")
    resp = urllib.request.urlopen(f"{base_url}/api/database/table/collection_cache?limit=5&offset=0", timeout=5)
    data = json.loads(resp.read())
    print(f"  Status: {resp.status}")
    print(f"  Total: {data.get('total')}")
    print(f"  Records count: {len(data.get('records', []))}")
    print("  OK")
except Exception as e:
    print(f"  FAILED: {e}")

# 测试4: Cookie表数据
try:
    print("\n=== 测试4: Cookie表数据 ===")
    resp = urllib.request.urlopen(f"{base_url}/api/database/table/cookie_cache?limit=5&offset=0", timeout=5)
    data = json.loads(resp.read())
    print(f"  Status: {resp.status}")
    print(f"  Total: {data.get('total')}")
    print(f"  Records count: {len(data.get('records', []))}")
    print("  OK")
except Exception as e:
    print(f"  FAILED: {e}")

# 测试5: 数据库统计
try:
    print("\n=== 测试5: 数据库统计 ===")
    resp = urllib.request.urlopen(f"{base_url}/api/database/stats", timeout=5)
    data = json.loads(resp.read())
    print(f"  Status: {resp.status}")
    print(f"  Stats: {data}")
    print("  OK")
except Exception as e:
    print(f"  FAILED: {e}")
