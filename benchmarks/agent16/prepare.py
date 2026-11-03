"""从指定 HumanEval checkout 导入数据；用 SHA256 排序固定抽样。"""
import gzip
import hashlib
import json
import subprocess
from pathlib import Path
import sys

ROOT = Path(__file__).parent
PIN = 'b9453e8f1865df0a0057a1383767caa400acfbcf'
SEED = 'lancher-agent16-20261103'

def prepare(checkout):
    checkout = Path(checkout)
    revision = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != PIN:
        raise ValueError('数据集 commit 与预注册版本不符')
    raw = (checkout / 'data/HumanEval.jsonl.gz').read_bytes()
    tasks = [json.loads(line) for line in gzip.decompress(raw).decode().splitlines()]
    ordered = sorted(tasks, key=lambda t: hashlib.sha256((SEED + ':' + t['task_id']).encode()).hexdigest())[:16]
    payload = ''.join(json.dumps(t, ensure_ascii=False, sort_keys=True) + '\n' for t in ordered).encode()
    (ROOT / 'data/tasks.jsonl').write_bytes(payload)
    (ROOT / 'data/LICENSE.human-eval').write_bytes((checkout / 'LICENSE').read_bytes())
    manifest = dict(schema_version=1, source='https://github.com/openai/human-eval', revision=revision,
                    source_sha256=hashlib.sha256(raw).hexdigest(), seed=SEED,
                    selection='sort ascending SHA256(seed + colon + task_id), take first 16; no filtering',
                    tasks_sha256=hashlib.sha256(payload).hexdigest(), task_ids=[t['task_id'] for t in ordered])
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

if __name__ == '__main__':
    prepare(sys.argv[1])
