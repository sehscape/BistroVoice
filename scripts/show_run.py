"""Print the transcripts of an evaluation run: python scripts/show_run.py voice B04 T01"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
tag, ids = sys.argv[1], set(sys.argv[2:])
path = ROOT / "runs" / f"{tag}.jsonl"
if not path.exists():
    path = ROOT / "eval" / "results" / tag / "scenarios.jsonl"
for line in path.read_text(encoding="utf-8").splitlines():
    r = json.loads(line)
    if ids and r["id"] not in ids:
        continue
    failed = [k for k, v in r["checks"].items() if not v]
    print(f"##### {r['id']} {r['title']} {'pass' if r['passed'] else 'FAIL ' + str(failed)}")
    for t in r["turns"]:
        rep = " (repeat)" if t.get("repeat") else ""
        print(f"  caller{rep}: {t['heard']!r}  [conf {t['confidence']}, stt {t.get('stt_ms')} ms]")
        for c in t["tool_calls"]:
            res = c["result"]
            out = res.get("status") or res.get("error") or ("available" if res.get("available") else
                                                             "not available" if "available" in res else "ok")
            print(f"     {'code' if c['forced'] else 'llm '} -> {c['name']}({json.dumps(c['args'])}) = {out}")
        if t["draft"] != t["reply"]:
            print(f"  draft : {t['draft']}")
        print(f"  agent : {t['reply']}   [llm {t['llm_ms']} ms x{t['llm_calls']}]")
        for e in t["guard_events"]:
            print(f"     guard: {e}")
    print(f"  rows  : {[(x['name'], x['party_size'], x['date'], x['time']) for x in r['new_reservations']]}")
