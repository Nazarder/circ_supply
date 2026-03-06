"""
cb_range_test.py
Test circuit breaker threshold sensitivity with ZEC excluded.
Uses the same book+ADTV slippage baseline as final_tests.py.
"""
import subprocess, sys, re, os

V9_PATH = "D:/AI_Projects/circ_supply/perpetual_ls_v9.py"

with open(V9_PATH, encoding="utf-8") as f:
    BASE = f.read()

BOOK_LOADER = (
    "\n"
    "import csv as _bk_csv\n"
    "_BOOK_SLIP = {}\n"
    "_ADTV_REF  = {}\n"
    "_BK_PATH = 'D:/AI_Projects/circ_supply/orderbook_slippage.csv'\n"
    "with open(_BK_PATH, newline='') as _bkf:\n"
    "    for _bkr in _bk_csv.DictReader(_bkf):\n"
    "        _sym = _bkr['symbol']\n"
    "        try:\n"
    "            _hs = float(_bkr['half_spread'])  if _bkr['half_spread']  else float('nan')\n"
    "            _im = float(_bkr['impact_$375K']) if _bkr['impact_$375K'] else float('nan')\n"
    "            _ar = float(_bkr['adtv_weekly'])  if _bkr['adtv_weekly']  else float('nan')\n"
    "            _BOOK_SLIP[_sym] = (_hs, _im)\n"
    "            _ADTV_REF[_sym]  = _ar\n"
    "        except (ValueError, KeyError):\n"
    "            pass\n"
)

OLD_SLIP = (
    "            slip   = sum(w[s] * float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)\n"
    "                         for s in syms)"
)

NEW_SLIP = (
    "            def _bk_slip_fn(s):\n"
    "                _pos = AUM * 0.75 / max(len(syms), 1)\n"
    "                if s in _BOOK_SLIP:\n"
    "                    _hs, _im = _BOOK_SLIP[s]\n"
    "                    _hs = 0.0 if (_hs != _hs) else float(_hs)\n"
    "                    _ar = float(_ADTV_REF.get(s, 0) or 0)\n"
    "                    _at = float(adtv_row.get(s, 0) if pd.notna(adtv_row.get(s)) else 0)\n"
    "                    _liq = min((_ar / _at) ** 0.5, 5.0) if (_ar > 0 and _at > 0) else 1.0\n"
    "                    _impact = _im * (max(_pos / 375000.0, 0.0) ** 0.5) * _liq if _im == _im else _hs * _liq\n"
    "                    return min(max(_hs, _impact), MAX_SLIPPAGE)\n"
    "                return float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)\n"
    "            slip   = sum(w[s] * _bk_slip_fn(s) for s in syms)"
)


def build(cb, zec_excl):
    s = BASE
    s = s.replace('warnings.filterwarnings("ignore")',
                  'warnings.filterwarnings("ignore")' + BOOK_LOADER, 1)
    s = s.replace("SLIPPAGE_K           = 0.0005",
                  "SLIPPAGE_K           = 0.0005\nAUM                  = 5000000", 1)
    s = s.replace(OLD_SLIP, NEW_SLIP, 1)
    s = s.replace("SHORT_CB_LOSS        = 0.40",
                  f"SHORT_CB_LOSS        = {cb}", 1)
    if zec_excl:
        s = s.replace("| COMMODITY_BACKED)", '| COMMODITY_BACKED | {"ZEC"})', 1)
    return s


def run(src, tag):
    tmp = f"D:/AI_Projects/circ_supply/_tmp_cb_{tag}.py"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    res = subprocess.run([sys.executable, tmp], capture_output=True, timeout=300)
    out = (res.stdout + res.stderr).decode("utf-8", errors="replace")
    try: os.remove(tmp)
    except OSError: pass
    m = re.search(
        r"L/S Combined \(net\)\s+([-+][\d.]+)%\s+[-+][\d.]+%"
        r"\s+[-+][\d.]+\s+([-+][\d.]+)\s+[-+][\d.]+\s+([-+][\d.]+)%", out)
    if m:
        return float(m.group(2)), float(m.group(1)), float(m.group(3))
    errs = [l for l in out.split("\n") if "rror" in l]
    print(f"    FAIL: {errs[-1][:80] if errs else out[-200:]}")
    return None, None, None


TESTS = [
    # label                          cb    zec_excl
    ("Baseline (with ZEC)",          0.40, False),
    ("ZEC excl, CB=40%",             0.40, True),
    ("ZEC excl, CB=50%",             0.50, True),
    ("ZEC excl, CB=60%",             0.60, True),
    ("ZEC excl, CB=80%",             0.80, True),
    ("ZEC excl, CB=OFF (1.0)",       1.00, True),
]

print("=" * 64)
print("  Circuit Breaker Range Test  |  Book+ADTV slippage, $5M AUM")
print("=" * 64)
print(f"  {'Config':<32}  {'SR(HAC)':>8}  {'Ann%':>7}  {'MDD%':>7}")
print("  " + "-" * 58)

for label, cb, zec in TESTS:
    sr, ann, mdd = run(build(cb, zec), label.replace(" ", "_")[:20])
    if sr is not None:
        print(f"  {label:<32}  {sr:>+8.3f}  {ann:>+7.1f}%  {mdd:>7.1f}%")

print("=" * 64)
