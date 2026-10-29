from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import date
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).with_name("config.json")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def atomic_json(path: Path, value, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config(path=DEFAULT_CONFIG):
    path = Path(path).resolve()
    c = json.loads(path.read_text())
    if c.get("schema_version") != 1 or c.get("research_only") is not True:
        raise ValueError("Only schema_version=1, research_only=true is supported")
    if date.fromisoformat(c["start_date"]) >= date.fromisoformat(c["end_date"]):
        raise ValueError("start_date must precede end_date")
    for name in ("data_dir", "runs_dir", "credentials_file"):
        c[name] = str((path.parent / c[name]).resolve())
    e, t, p = c["execution"], c["training"], c["pattern"]
    if not 1 <= e["max_positions"] <= 3 or not isinstance(e["max_positions"], int):
        raise ValueError("max_positions must be an integer in [1,3]")
    for k in ("buy_fee_bps", "sell_fee_bps", "sell_tax_bps", "slippage_bps", "half_spread_bps"):
        if not isinstance(e[k], (float, int)) or not math.isfinite(e[k]) or not 0 <= e[k] <= 1000:
            raise ValueError(f"Invalid execution.{k}")
    for k in ("initial_cash_krw", "max_position_krw", "max_holding_minutes"):
        if e[k] <= 0:
            raise ValueError(f"Invalid execution.{k}")
    if not 0 < e["max_initial_risk_pct"] <= .02 or not 0 < e["participation"] <= 1:
        raise ValueError("Invalid risk / participation")
    if not 0 < e["daily_loss_limit_pct"] < 1 or not 0 < e["breakeven_trigger_pct"] < .03:
        raise ValueError("Invalid daily loss / breakeven trigger")
    if e["max_position_krw"] > e["initial_cash_krw"] or not 0 < t["holdout_fraction"] < .4:
        raise ValueError("Invalid cash / holdout_fraction")
    if t["cv_folds"] < 2 or min(t["trials"], t["threads"], t["min_candidates"], t["min_days"]) < 1:
        raise ValueError("Invalid training configuration")
    if p["pivot_right_bars"] < 1 or p["lookback_bars"] < 20 or min(p.values()) <= 0:
        raise ValueError("Invalid pattern configuration")
    c["config_path"] = str(path)
    return c
