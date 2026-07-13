import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app.core.config import Config
from app.core.feishu import FeishuClient
from app.core.syncer_v2 import Syncer as SyncerV2

config = Config()
fc = config.feishu

client = FeishuClient(fc['app_id'], fc['app_secret'])
collector = None  # step1 doesn't need collector

syncer = SyncerV2(client, collector, fc)

# Check feishu_syncer exists
print(f'feishu_syncer: {syncer.feishu_syncer}')
print(f'collection_table_id: {syncer.feishu_syncer.collection_table_id if syncer.feishu_syncer else "N/A"}')

# Run step1 with empty text
result = syncer.import_to_collection('')
print(f'\nStep1 result: {result.to_dict()}')

# Check DB
from app.core.database import Database
db = Database()
rows = db.get_all_collections()
print(f'\nDB collection_cache: {len(rows)} rows')
if rows:
    r = rows[0]
    print(f'First row: share={r.get(chr(0x5206)+chr(0x4eab)+chr(0x7801))}, sec={r.get(chr(0x8d26)+chr(0x53f7)+chr(0x6807)+chr(0x8bc6))}, fans={r.get(chr(0x7c89)+chr(0x4e1d)+chr(0x6570))}, name={r.get(chr(0x660a)+chr(0x79f0))}')

client.close()
