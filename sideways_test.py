"""
sideways_test.py
================
Tests whether the Sideways=cash rule is load-bearing or incidental.
Key question: does the strategy *need* to go to cash in Sideways, or does
the supply signal work well enough to trade through it?

Configs:
  baseline    — Sideways=cash (current v8)
  sw_075      — Sideways=(0.75, 0.75) same as Bull/Bear
  sw_050      — Sideways=(0.50, 0.50) half exposure
  sw_025      — Sideways=(0.25, 0.25) quarter exposure
  sw_inv      — Sideways=(0.00, 0.00) BUT Bull/Bear also forced to 0 (pure sideways-only control)
  sw_only     — trade ONLY in Sideways periods (flip: cash in Bull/Bear)
"""
import sys, os, re, subprocess, tempfile
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")

V7_PATH = "D:/AI_Projects/circ_supply/perpetual_ls_v7.py"
with open(V7_PATH, encoding="utf-8") as f:
    BASE = f.read()

V8 = {"BULL_BAND": "1.05", "BEAR_BAND": "0.95",
      "SUPPLY_WINDOW": "26", "LONG_QUALITY_LOOKBACK": "12"}

OLD_SCALE = """REGIME_LS_SCALE = {
    ("Sideways", False): (0.00, 0.00),   # [V7-2] hold cash
    ("Sideways", True):  (0.00, 0.00),   # [V7-2] hold cash
    ("Bull",     False): (0.75, 0.75),   # [V7-6] symmetric L/S, same as Bear
    ("Bull",     True):  (0.50, 0.25),   # high-vol bull: scale back
    ("Bear",     False): (0.75, 0.75),   # unchanged from v4/v6"""

def make_scale(bull_f, bull_t, bear_f, side_f):
    return f"""REGIME_LS_SCALE = {{
    ("Sideways", False): ({side_f}, {side_f}),
    ("Sideways", True):  ({side_f}, {side_f}),
    ("Bull",     False): ({bull_f}, {bull_f}),
    ("Bull",     True):  ({bull_t}, {bull_t}),
    ("Bear",     False): ({bear_f}, {bear_f}),"""

def param_patch(s, ov):
    for k, v in ov.items():
        pat = rf"^({re.escape(k)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$"
        s = re.sub(pat, rf"\g<1>{v}\g<3>", s, flags=re.MULTILINE)
    return s

def suppress(s):
    s = s.replace("plt.savefig", "pass")
    return s.replace('print(f"[Plot]', 'pass  #')

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

def parse(stdout):
    nan = float("nan")
    if not stdout or stdout.startswith("__"): return nan, nan, nan, 0
    def find(pat):
        m = re.search(pat, stdout)
        return float(m.group(1).replace('%','').replace('+','')) if m else nan
    ann    = find(r"L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%")
    sharpe = find(r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)")
    maxdd  = find(r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+"
                  r"[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+([\-\+]\d+\.\d+)%")
    m = re.search(r"Rebalancing periods\s*:\s*(\d+)", stdout)
    periods = int(m.group(1)) if m else 0
    return ann, sharpe, maxdd, periods

# Build sources
BASE_V8 = suppress(param_patch(BASE, V8))

CONFIGS = [
    ("baseline  (Sideways=cash)",  BASE_V8),  # no scale patch needed
    ("sw_075    (Sideways=0.75)",  suppress(param_patch(BASE.replace(OLD_SCALE, make_scale("0.75","0.50","0.75","0.75")), V8))),
    ("sw_050    (Sideways=0.50)",  suppress(param_patch(BASE.replace(OLD_SCALE, make_scale("0.75","0.50","0.75","0.50")), V8))),
    ("sw_025    (Sideways=0.25)",  suppress(param_patch(BASE.replace(OLD_SCALE, make_scale("0.75","0.50","0.75","0.25")), V8))),
    ("sw_only   (trade Sideways, cash Bull/Bear)",
                                   suppress(param_patch(BASE.replace(OLD_SCALE, make_scale("0.00","0.00","0.00","0.75")), V8))),
]

print("=" * 75)
print("SIDEWAYS REGIME TEST — is Sideways=cash load-bearing?")
print("All configs: v8 params, only Sideways exposure varies.")
print("=" * 75)
print()
print(f"  {'Config':<38} {'Ann':>8} {'Sharpe':>8} {'MaxDD':>8} {'Perds':>6} {'dSR':>8}")
print("  " + "-" * 82)

base_sr = None
for label, src in CONFIGS:
    out = run_src(src)
    ann, sr, dd, n = parse(out)
    if base_sr is None:
        base_sr = sr
        dsr_str = "        "
        flag = ""
    else:
        dsr = sr - base_sr
        dsr_str = f"{dsr:>+7.3f}"
        flag = " <<< HURTS" if dsr < -0.10 else (" >>> HELPS" if dsr > 0.10 else "")
    print(f"  {label:<38} {ann:>+7.2f}%  {sr:>+7.3f}  {dd:>+7.2f}%  {n:>5}  {dsr_str}{flag}",
          flush=True)

print()
print("  Interpretation:")
print("  - If sw_075 hurts badly -> Sideways=cash is load-bearing (regime filter is the real edge)")
print("  - If sw_075 is neutral/helps -> the signal works in Sideways too, cash rule is conservative")
print("  - If sw_only beats baseline -> Sideways is actually the BEST regime for the signal")
print()
print("Done.")
