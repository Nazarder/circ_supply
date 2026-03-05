"""
perm_v8.py — Permutation test on v8 baseline (altcoin L/S signal)
Null: long N random alts, short N different random alts.
Real: long lowest-inflation, short highest-inflation.
Tests whether the cross-sectional supply signal adds value on BOTH sides.
"""
import sys, os, re, subprocess, tempfile
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")

V7_PATH = "D:/AI_Projects/circ_supply/perpetual_ls_v7.py"
N_PERMS = 200

with open(V7_PATH, encoding="utf-8") as f:
    BASE = f.read()

V8 = {
    "BULL_BAND":             "1.05",
    "BEAR_BAND":             "0.95",
    "SUPPLY_WINDOW":         "26",
    "LONG_QUALITY_LOOKBACK": "12",
}

def patch(s, ov):
    for k, v in ov.items():
        pat = rf"^({re.escape(k)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$"
        s = re.sub(pat, rf"\g<1>{v}\g<3>", s, flags=re.MULTILINE)
    return s

def suppress(s):
    s = s.replace("plt.savefig", "pass  # plt.savefig")
    return s.replace('print(f"[Plot]', 'pass  # print(f"[Plot]')

def run_src(source, timeout=360):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(source); tmp = f.name
    try:
        r = subprocess.run([sys.executable, tmp], capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
        return r.stdout if r.returncode == 0 else "__ERROR__\n" + r.stderr[-400:]
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    finally:
        os.unlink(tmp)

def parse_sharpe(stdout):
    if not stdout or stdout.startswith("__"): return float("nan")
    m = re.search(
        r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)",
        stdout)
    return float(m.group(1)) if m else float("nan")

def parse_ann(stdout):
    if not stdout or stdout.startswith("__"): return float("nan")
    m = re.search(r"L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%", stdout)
    return float(m.group(1)) if m else float("nan")

V8_SRC = suppress(patch(BASE, V8))

print("=" * 70)
print("CORE THESIS PERMUTATION TEST — v8 Altcoin L/S Signal")
print("Null: long random alts vs short random alts (both sides shuffled)")
print("Real: long lowest-inflation vs short highest-inflation alts")
print("=" * 70)

print("\nRunning v8 real signal...", flush=True)
real_out = run_src(V8_SRC)
real_sr  = parse_sharpe(real_out)
real_ann = parse_ann(real_out)
print(f"v8 real signal: SR={real_sr:>+.3f}  Ann={real_ann:>+.2f}%\n")

print(f"Running {N_PERMS} permuted simulations...", flush=True)
perm_srs = []
for seed in range(N_PERMS):
    out = run_src(patch(V8_SRC, {"PERMUTE_SEED": str(seed)}))
    sr  = parse_sharpe(out)
    perm_srs.append(sr)
    if (seed + 1) % 20 == 0:
        valid = [x for x in perm_srs if not np.isnan(x)]
        print(f"  {seed+1}/{N_PERMS} | mean={np.mean(valid):>+.3f} | "
              f"above real={sum(x >= real_sr for x in valid)}/{len(valid)}", flush=True)

valid = np.array([x for x in perm_srs if not np.isnan(x)])
p_val = (valid >= real_sr).mean()

print()
print("RESULTS:")
print(f"  Real v8 Sharpe              : {real_sr:>+.3f}")
print(f"  Permuted mean Sharpe        : {np.mean(valid):>+.3f}")
print(f"  Permuted std Sharpe         : {np.std(valid):>.3f}")
print(f"  Permuted 95th pct           : {np.percentile(valid, 95):>+.3f}")
print(f"  Permuted 99th pct           : {np.percentile(valid, 99):>+.3f}")
print(f"  Sims >= real SR             : {(valid >= real_sr).sum()}/{len(valid)}")
print(f"  Empirical p-value           : {p_val:.4f}")
print(f"  Real SR percentile in null  : {(valid < real_sr).mean()*100:.1f}th")
print()
if p_val < 0.01:
    print("Verdict: CROSS-SECTIONAL SUPPLY SIGNAL HAS ALPHA (p<0.01)")
    print("  Low-inflation longs vs high-inflation shorts beats random pair selection.")
elif p_val < 0.05:
    print("Verdict: Marginally significant (p<0.05) — signal above random at 5% level.")
elif p_val < 0.10:
    print("Verdict: BORDERLINE (p<0.10) — weak evidence the signal beats random.")
else:
    print("Verdict: FAIL — cross-sectional supply signal NOT distinguishable from")
    print("  randomly picking long and short baskets from the universe.")
print("\nDone.")
