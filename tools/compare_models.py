"""3モデルで同一プロンプトの台本を生成して比較（一時ツール）。"""
import json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from doci import corners
from doci.ai_text import _extract_json

OUT = Path(__file__).resolve().parent.parent / "output" / "model_compare"
OUT.mkdir(parents=True, exist_ok=True)
CORNER = corners.CORNERS["communism"]
PROMPT = corners.build_prompt(CORNER, "2026-05-31", [])

def run_claude(model):
    p = subprocess.run(["claude","-p",PROMPT,"--model",model,"--output-format","json"],
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"rc={p.returncode}: {p.stderr[:300]}")
    env = json.loads(p.stdout)
    return env.get("result", p.stdout) if isinstance(env, dict) else p.stdout

def run_opencode(model):
    p = subprocess.run(["opencode","run","-m",model,PROMPT],
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"rc={p.returncode}: {p.stderr[:300]}")
    return p.stdout

JOBS = {
    "opus":           ("claude",  "claude-opus-4-8"),
    "deepseek4flash": ("opencode","opencode-go/deepseek-v4-flash"),
    "deepseek4pro":   ("opencode","opencode-go/deepseek-v4-pro"),
    "glm5":           ("opencode","opencode-go/glm-5"),
    "glm51":          ("opencode","opencode-go/glm-5.1"),
    "kimi25":         ("opencode","opencode-go/kimi-k2.5"),
    "kimi26":         ("opencode","opencode-go/kimi-k2.6"),
    "mimo25":         ("opencode","opencode-go/mimo-v2.5"),
    "mimo25pro":      ("opencode","opencode-go/mimo-v2.5-pro"),
    "minimax25":      ("opencode","opencode-go/minimax-m2.5"),
    "minimax27":      ("opencode","opencode-go/minimax-m2.7"),
    "minimax3":       ("opencode","opencode-go/minimax-m3"),
    "qwen36":         ("opencode","opencode-go/qwen3.6-plus"),
    "qwen37max":      ("opencode","opencode-go/qwen3.7-max"),
    "qwen37plus":     ("opencode","opencode-go/qwen3.7-plus"),
}

def work(name):
    kind, model = JOBS[name]
    t0 = time.time()
    try:
        raw = (run_claude if kind=="claude" else run_opencode)(model)
        script = _extract_json(raw)
        dt = time.time() - t0
        (OUT / f"{name}.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / f"{name}.raw.txt").write_text(raw, encoding="utf-8")
        return name, {"ok": True, "sec": round(dt,1), "model": model,
                      "title": script.get("title",""),
                      "narration": script.get("narration",""),
                      "n_chars": len(script.get("narration","")),
                      "n_scenes": len(script.get("scenes",[]))}
    except Exception as e:
        return name, {"ok": False, "sec": round(time.time()-t0,1), "model": model, "error": str(e)[:400]}

with ThreadPoolExecutor(max_workers=5) as ex:
    results = dict(ex.map(work, JOBS.keys()))

print("="*70)
for name in JOBS:
    r = results[name]
    print(f"\n### {name}  ({r['model']})  [{r['sec']}s]")
    if not r["ok"]:
        print(f"  ERROR: {r['error']}"); continue
    print(f"  title: {r['title']}")
    print(f"  narration: {r['n_chars']}字 / scenes: {r['n_scenes']}")
    print(f"  ---\n  {r['narration']}")
(OUT / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n保存先:", OUT)
