"""固定公开数据版本，并按 SHA256 排序选取16题，避免依赖随机数实现。"""
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE_SHA = 'b9453e8f1865df0a0057a1383767caa400acfbcf'
SEED = 'lancher-comparison-v1-2026-11-03'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def prepare(source):
    source = Path(source)
    sha = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if sha != SOURCE_SHA:
        raise ValueError('数据源提交不匹配')
    raw = (source / 'data/HumanEval.jsonl.gz').read_bytes()
    rows = [json.loads(line) for line in gzip.decompress(raw).splitlines()]
    ranked = sorted(rows, key=lambda r: digest((SEED + ':' + r['task_id']).encode()))[:16]
    payload = ''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in ranked).encode()
    (ROOT / 'data/tasks.jsonl').write_bytes(payload)
    manifest = dict(dataset='openai/human-eval', source_commit=sha, source_sha256=digest(raw),
        selection='SHA256(seed + colon + task_id) ascending; first 16', seed=SEED,
        tasks_sha256=digest(payload), task_ids=[r['task_id'] for r in ranked],
        lancher_commit='f984bb72b46d8eff8d82d148782da83a62a3d308', baseline='mini-swe-agent==2.4.6',
        max_rounds=50, repetitions=1, grading='Original check(candidate), all assertions must pass',
        scope='function-level; not SWE-bench or repository repair', online_status='not_run')
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))

if __name__ == '__main__':
    prepare(sys.argv[1])
