from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import VERSION
from .config import atomic_json, digest
from .execution import outcome, portfolio
from .features import FEATURES, regular_bars, contract_hashes


def read_dataset(config):
    root = Path(config["data_dir"])
    manifest = json.loads((root / "dataset.json").read_text())
    if manifest["version"] != VERSION or manifest["feature_names"] != FEATURES:
        raise ValueError("Dataset/feature version mismatch; rebuild")
    if manifest.get("contract_hashes") != contract_hashes():
        raise ValueError("Feature/label implementation changed; rebuild dataset before training or inference")
    actual_hash = hashlib.sha256((root / "candidates.parquet").read_bytes()).hexdigest()
    if actual_hash != manifest["dataset_sha256"]:
        raise ValueError("Dataset checksum mismatch")
    if manifest["pattern"] != config["pattern"] or manifest["execution"] != config["execution"]:
        raise ValueError("Dataset pattern/execution configuration changed; rebuild")
    frame = pd.read_parquet(root / "candidates.parquet")
    for k in ("timestamp", "entry_at", "exit_at"):
        frame[k] = pd.to_datetime(frame[k], utc=True)
    return frame, manifest


def split_dates(days, config):
    days = sorted(set(days))
    t = config["training"]
    if len(days) < t["min_days"]:
        raise ValueError(f"Need at least {t['min_days']} market days, got {len(days)}")
    cut = len(days) - max(1, int(np.ceil(len(days) * t["holdout_fraction"])))
    dev, holdout = days[:cut], days[cut:]
    bounds = np.linspace(len(dev) // 2, len(dev), t["cv_folds"] + 1).astype(int)
    folds = [(dev[:left], dev[left:right]) for left, right in zip(bounds[:-1], bounds[1:])]
    if any(not train or not valid for train, valid in folds):
        raise ValueError("Not enough days for chronological folds")
    return dev, holdout, folds


def fitting_rows(frame, train_days, boundary_day):
    cutoff = pd.Timestamp(boundary_day, tz="Asia/Seoul").tz_convert("UTC")
    mask = frame.day.isin(train_days) & frame.filled & frame.net_R.notna() & (frame.exit_at < cutoff)
    return frame.loc[mask].copy()


def weights(frame):
    w = 1 / frame.groupby("day").day.transform("size").to_numpy(dtype=float)
    return w / w.mean()


def linear_model():
    return make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(), Ridge(alpha=10.))


def tree_model(params, config, estimators=None, seed=None):
    return lgb.LGBMRegressor(objective="huber", n_estimators=estimators or config["training"]["max_estimators"],
                             learning_rate=params["learning_rate"], num_leaves=params["num_leaves"], max_depth=params["max_depth"],
                             min_child_samples=params["min_child_samples"], reg_lambda=params["reg_lambda"],
                             colsample_bytree=params["colsample_bytree"], random_state=seed or config["training"]["seed"],
                             n_jobs=config["training"]["threads"], verbosity=-1, deterministic=True, force_col_wise=True)


def interval_summary(frame, prediction, threshold, config, days):
    result = portfolio(frame, prediction, threshold, config, all_days=days)
    end = pd.Timestamp(days[-1] + "T15:20:00", tz="Asia/Seoul").tz_convert("UTC")
    known = frame.filled & frame.net_R.notna() & (frame.exit_at <= end)
    result["mae_net_R"] = float(mean_absolute_error(frame.loc[known, "net_R"], np.asarray(prediction)[known])) if known.any() else None
    result["candidate_rows"] = len(frame)
    result["label_status_counts"] = frame.label_status.value_counts().to_dict()
    result["regime_candidate_counts"] = frame.regime.value_counts().to_dict()
    return result


def compact(result):
    return {k: v for k, v in result.items() if k not in ("trade_log", "daily_pnl")}


def optimize(config, diagnostic=False):
    frame, manifest = read_dataset(config)
    if manifest["partial"] and not diagnostic:
        raise ValueError("Partial datasets require --diagnostic; they cannot produce a final evaluated model")
    if len(frame) < config["training"]["min_candidates"]:
        raise ValueError("Insufficient pattern candidates for training")
    dev_days, holdout_days, fold_days = split_dates(manifest["days"], config)
    fold_data = []
    for train_days, valid_days in fold_days:
        train = fitting_rows(frame, train_days, valid_days[0])
        valid = frame.loc[frame.day.isin(valid_days)].copy()
        if len(train) < 50 or int(valid.filled.sum()) < 10:
            raise ValueError("Insufficient filled examples in a temporal fold")
        fold_data.append((train, valid, valid_days))
    run = Path(config["runs_dir"]) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ("-diagnostic" if diagnostic else ""))
    run.mkdir(parents=True)
    atomic_json(run / "config.json", config)
    atomic_json(run / "dataset.json", manifest)
    atomic_json(run / "splits.json", {"development_days": dev_days, "holdout_days": holdout_days,
                "folds": [{"train_days": train, "validation_days": valid} for train, valid in fold_days],
                "label_boundary": "Only fully closed labels with exit_at before the next period are fitted"})
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    storage = optuna.storages.RDBStorage(url=f"sqlite:///{run / 'optimization.sqlite'}")
    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=config["training"]["seed"]),
                                storage=storage, study_name="minute-model")

    def objective(trial):
        depth = trial.suggest_categorical("max_depth", [3, 5, 7])
        leaves = trial.suggest_categorical("num_leaves", [7, 15, 31])
        if leaves > 2 ** depth:
            raise optuna.TrialPruned()
        params = {"max_depth": depth, "num_leaves": leaves,
                  "learning_rate": trial.suggest_float("learning_rate", .02, .1, log=True),
                  "min_child_samples": trial.suggest_categorical("min_child_samples", [20, 100, 300]),
                  "reg_lambda": trial.suggest_categorical("reg_lambda", [0., 1., 10.]),
                  "colsample_bytree": trial.suggest_categorical("colsample_bytree", [.7, 1.])}
        threshold = trial.suggest_categorical("threshold", [-.1, 0., .1, .25, .5])
        fold_scores, summaries, rounds, total_trades = [], [], [], 0
        for i, (train, valid, days) in enumerate(fold_data):
            model = tree_model(params, config)
            next_day = (pd.Timestamp(days[-1]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            validation = fitting_rows(valid, days, next_day)
            model.fit(train[FEATURES], train.net_R, sample_weight=weights(train),
                      eval_X=validation[FEATURES], eval_y=validation.net_R,
                      callbacks=[lgb.early_stopping(config["training"]["early_stopping_rounds"], verbose=False)])
            pred = model.predict(valid[FEATURES])
            summary = interval_summary(valid, pred, threshold, config, days)
            rounds.append(int(model.best_iteration_ or config["training"]["max_estimators"]))
            daily = np.array(list(summary["daily_pnl"].values()))
            fold_scores.append(float(daily.mean()))
            total_trades += summary["trades"]
            summaries.append(compact(summary))
            trial.report(float(np.mean(fold_scores)), i)
            if trial.should_prune():
                raise optuna.TrialPruned()
        trial.set_user_attr("rounds", rounds)
        trial.set_user_attr("folds", summaries)
        trial.set_user_attr("total_trades", total_trades)
        trial.set_user_attr("activity_qualified", total_trades >= config["training"]["min_validation_trades"])
        mean_mae = float(np.mean([s["mae_net_R"] for s in summaries]))
        trial.set_user_attr("mean_mae_net_R", mean_mae)
        economic_score = float(np.mean(fold_scores) - .25 * np.std(fold_scores))
        trial.set_user_attr("economic_score", economic_score)
        if not trial.user_attrs["activity_qualified"]:
            return -1e9 - mean_mae
        return economic_score

    try:
        study.optimize(objective, n_trials=config["training"]["trials"], n_jobs=1,
                       callbacks=[lambda _, trial: print(f"trial {trial.number + 1} {trial.state.name} score={trial.value}", flush=True)])
        trials, trial_table = study.trials, study.trials_dataframe()
    finally:
        storage.remove_session()
        storage.engine.dispose()
    completed = [t for t in trials if t.state == optuna.trial.TrialState.COMPLETE]
    if not completed:
        raise ValueError("No trial completed; inspect training errors")
    qualified = [t for t in completed if t.user_attrs["activity_qualified"]]
    best = max(qualified or completed, key=lambda t: t.value)
    params = {k: v for k, v in best.params.items() if k != "threshold"}
    policy_active = bool(qualified)
    threshold = best.params["threshold"] if policy_active else 1e12
    rounds = int(np.median(best.user_attrs["rounds"]))
    out_of_fold, reports = [], []
    for train, valid, days in fold_data:
        model = tree_model(params, config, estimators=rounds)
        model.fit(train[FEATURES], train.net_R, sample_weight=weights(train))
        linear = linear_model().fit(train[FEATURES], train.net_R, ridge__sample_weight=weights(train))
        scores, linear_scores = model.predict(valid[FEATURES]), linear.predict(valid[FEATURES])
        reports.append({"train_first": train.day.min(), "train_last": train.day.max(), "valid_first": days[0], "valid_last": days[-1],
                        "tree": compact(interval_summary(valid, scores, threshold, config, days)),
                        "linear": compact(interval_summary(valid, linear_scores, 0., config, days)),
                        "rule": compact(interval_summary(valid, np.zeros(len(valid)), 0., config, days))})
        oof = valid.copy()
        oof["supervised_score"], oof["linear_score"] = scores, linear_scores
        out_of_fold.append(oof)
    pd.concat(out_of_fold, ignore_index=True).to_parquet(run / "development-predictions.parquet", index=False)
    training = fitting_rows(frame, dev_days, holdout_days[0])
    model = tree_model(params, config, estimators=rounds)
    model.fit(training[FEATURES], training.net_R, sample_weight=weights(training))
    linear = linear_model().fit(training[FEATURES], training.net_R, ridge__sample_weight=weights(training))
    if manifest["contract_hashes"] != contract_hashes():
        raise ValueError("Feature/label source changed during training; rebuild and retrain")
    bundle = {"version": VERSION, "contract_hashes": contract_hashes(), "python_version": sys.version,
              "features": FEATURES, "model": model, "linear_model": linear,
              "threshold": threshold, "policy_active": policy_active, "trained_through": dev_days[-1], "holdout_days": holdout_days,
              "config": config, "dataset_sha256": manifest["dataset_sha256"], "diagnostic": bool(diagnostic),
              "rounds": rounds, "params": params}
    joblib.dump(bundle, run / "model.joblib")
    model.booster_.save_model(str(run / "lightgbm.txt"))
    importance = pd.DataFrame({"feature": FEATURES, "gain": model.booster_.feature_importance("gain"),
                               "splits": model.booster_.feature_importance("split")})
    gain_total = importance.gain.sum()
    importance["gain_fraction"] = importance.gain / gain_total if gain_total > 0 else 0.
    importance.sort_values("gain", ascending=False).to_csv(run / "feature-importance.csv", index=False)
    model_hash = hashlib.sha256((run / "model.joblib").read_bytes()).hexdigest()
    report = {"status": "trained_diagnostic_only" if diagnostic else "trained_not_final_evaluated", "model_sha256": model_hash,
              "source": manifest["source"], "diagnostic": bool(diagnostic), "threshold": threshold, "policy_active": policy_active,
              "policy_note": "Activity criterion met; profitability still requires independent evaluation" if policy_active else "No policy met minimum validation trade count; inference scores available, entries disabled",
              "best_params": params,
              "feature_importance_file": "feature-importance.csv",
              "boosting_rounds": rounds, "optimization_objective": "Activity-qualified: mean fold daily net PnL minus 0.25 * std of fold means. Otherwise: -1e9 minus prediction MAE; disable entries",
              "trials": len(trials), "completed_trials": len(completed), "development_folds": reports,
              "holdout_first": holdout_days[0], "holdout_last": holdout_days[-1], "holdout_accessed": False,
              "dependencies": {name: importlib.metadata.version(name) for name in ("numpy", "pandas", "scikit-learn", "lightgbm", "optuna")},
              "python_version": sys.version, "contract_hashes": contract_hashes(),
              "uv_lock_sha256": hashlib.sha256(Path(__file__).parents[1].joinpath("uv.lock").read_bytes()).hexdigest(),
              "limitations": manifest["limitations"], "costs_verified": config["execution"]["costs_verified"]}
    atomic_json(run / "training.json", report)
    trial_table.to_csv(run / "trials.csv", index=False)
    atomic_json(Path(config["runs_dir"]) / "latest.json", {"run_dir": str(run), "model_sha256": model_hash, "status": report["status"]})
    return {"run_dir": str(run), **report}


def load_bundle(run):
    run = Path(run)
    report = json.loads((run / "training.json").read_text())
    if hashlib.sha256((run / "model.joblib").read_bytes()).hexdigest() != report["model_sha256"]:
        raise ValueError("Model checksum mismatch")
    # Only load locally generated, checksum-verified models; joblib is not a sandbox.
    bundle = joblib.load(run / "model.joblib")
    if bundle["version"] != VERSION or bundle["features"] != FEATURES:
        raise ValueError("Model feature/version mismatch")
    if bundle.get("contract_hashes") != contract_hashes():
        raise ValueError("Feature implementation changed or legacy model; rebuild and retrain")
    return bundle


def bootstrap_difference(model_daily, rule_daily, seed, samples=1000):
    days = sorted(set(model_daily) | set(rule_daily))
    delta = np.array([model_daily.get(d, 0.) - rule_daily.get(d, 0.) for d in days])
    if not len(delta):
        return None
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(samples):
        starts = rng.integers(0, len(delta), size=int(np.ceil(len(delta) / 5)))
        indices = np.concatenate([(s + np.arange(5)) % len(delta) for s in starts])[:len(delta)]
        means.append(float(delta[indices].mean()))
    return {"unit": "KRW/day", "mean": float(delta.mean()), "ci95": np.quantile(means, [.025, .975]).tolist(),
            "method": "paired circular trading-day block bootstrap, block=5", "samples": samples}


def stress_labels(frame, config, multiplier=1., delay=0):
    parts = []
    for symbol, group in frame.groupby("symbol"):
        bars = regular_bars(pd.read_parquet(Path(config["data_dir"]) / "candles" / f"{symbol}-1m-raw.parquet"))[["timestamp", "open", "high", "low", "close", "volume"]]
        for row in group.to_dict("records"):
            label = outcome(bars, row["timestamp"], row["stop_price"], config,
                            delay_bars=delay, cost_multiplier=multiplier)
            # Clear old outcomes when the stressed entry becomes unfilled.
            row.update({k: None for k in ("entry_at", "exit_at", "net_pnl", "net_R", "capital")})
            row.update({"path_times": [], "path_pnl": [], "path_remaining": [], "path_ambiguous": [], **label})
            parts.append(row)
    result = pd.DataFrame(parts).sort_values(["timestamp", "symbol"])
    result["exit_at"] = pd.to_datetime(result.exit_at, utc=True)
    return result


def evaluate(run):
    run = Path(run)
    bundle = load_bundle(run)
    if bundle["diagnostic"]:
        raise ValueError("Diagnostic/partial models cannot consume the final holdout")
    config = bundle["config"]
    frame, manifest = read_dataset(config)
    if manifest["dataset_sha256"] != bundle["dataset_sha256"]:
        raise ValueError("Dataset changed after training")
    test = frame.loc[frame.day.isin(bundle["holdout_days"])].copy()
    key = digest({"holdout_days": bundle["holdout_days"], "symbols": sorted(c["symbol"] for c in manifest["coverage"])})
    model_hash = hashlib.sha256((run / "model.joblib").read_bytes()).hexdigest()
    ledger_path = Path(config["runs_dir"]) / "holdout-access.sqlite"
    with closing(sqlite3.connect(ledger_path)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS access(key TEXT PRIMARY KEY, model_hash TEXT, run_dir TEXT)")
        with db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT model_hash,run_dir FROM access WHERE key=?", (key,)).fetchone()
            if existing and existing[0] != model_hash:
                raise ValueError("This holdout was already opened for another model. Obtain a new future test period")
            db.execute("INSERT OR IGNORE INTO access VALUES(?,?,?)", (key, model_hash, str(run)))
    if (run / "evaluation.json").exists():
        return json.loads((run / "evaluation.json").read_text())
    prediction = bundle["model"].predict(test[FEATURES])
    test["score"] = prediction
    main = interval_summary(test, prediction, bundle["threshold"], config, bundle["holdout_days"])
    rule = interval_summary(test, np.zeros(len(test)), 0., config, bundle["holdout_days"])
    linear = interval_summary(test, bundle["linear_model"].predict(test[FEATURES]), 0., config, bundle["holdout_days"])
    stressed = stress_labels(test, config, multiplier=2.)
    doubled_cost = interval_summary(stressed, stressed.score, bundle["threshold"], config, bundle["holdout_days"])
    delayed = stress_labels(test, config, delay=1)
    delayed_result = interval_summary(delayed, delayed.score, bundle["threshold"], config, bundle["holdout_days"])
    ci = bootstrap_difference(main["daily_pnl"], rule["daily_pnl"], config["training"]["seed"])
    report = {"status": "research_evaluated", "live_approved": False, "source": manifest["source"], "model_sha256": model_hash,
              "policy_active": bundle["policy_active"], "threshold": bundle["threshold"],
              "holdout_first": bundle["holdout_days"][0], "holdout_last": bundle["holdout_days"][-1],
              "tree": main, "linear": linear, "rule": rule, "cash_baseline_pnl_krw": 0.,
              "double_cost": doubled_cost, "one_bar_delay": delayed_result, "improvement_vs_rule": ci,
              "costs_verified": config["execution"]["costs_verified"], "limitations": manifest["limitations"],
              "next_gate": "Fresh real-time paper observation and verified fees/taxes; no automatic live promotion"}
    test[["timestamp", "symbol", "regime", "score", "filled", "label_status", "net_R"]].to_parquet(run / "holdout-predictions.parquet", index=False)
    atomic_json(run / "evaluation.json", report)
    latest_path = Path(config["runs_dir"]) / "latest.json"
    if latest_path.exists():
        latest = json.loads(latest_path.read_text())
        if latest["model_sha256"] == model_hash:
            atomic_json(latest_path, {**latest, "status": report["status"]})
    policy_note = "검증 거래 수 기준 충족. 수익성은 아래 최종 평가에서 확인해야 한다." if bundle["policy_active"] else "검증 거래 수 기준 미달로 신규 진입 비활성화. 예측 점수만 계산하며, 무거래 손익 0은 수익성 증거가 아니다. 현금 보유와 같은 결과이므로 규칙 대비 차이를 모델의 예측 우위로 해석하지 않는다."
    text = ["# 반도체 분봉 모델 연구 평가", "", "실거래 승인이나 실제 체결 성과가 아닌 비용 가정 기반 연구 결과다.", "",
            policy_note, "",
            f"최종 기간: {report['holdout_first']} ~ {report['holdout_last']}", "", "| 전략 | 순손익 KRW | 최대 낙폭 % | 거래 수 |", "|---|---:|---:|---:|"]
    for name, value in (("LightGBM", main), ("선형", linear), ("규칙", rule), ("비용 2배", doubled_cost), ("한 봉 지연", delayed_result)):
        text.append(f"| {name} | {value['net_pnl_krw']:.0f} | {value['max_drawdown_pct']:.2f} | {value['trades']} |")
    text.append("| 현금 보유 | 0 | 0.00 | 0 |")
    text.extend(["", f"규칙 대비 일평균 손익 차이의 95% 블록 신뢰구간: {ci['ci95']}", "", *[f"- {x}" for x in report["limitations"]]])
    (run / "REPORT.md").write_text("\n".join(text) + "\n")
    return report
