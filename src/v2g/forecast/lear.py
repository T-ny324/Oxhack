"""LEAR price forecasts (stage S3, ladder step 2): lasso-estimated autoregressive
model of Lago, Marcjasz, De Schutter & Weron (2021), adapted to half-hourly GB
prices with a 10:00 local issue and a ~38 h horizon.

Run
---
::

    python -m v2g.forecast.lear --window smoke|backtest [--start --end] \\
        [--recalibrate-every N] [--windows 56,84,364,728]
    make forecast-lear          # backtest window
    make forecast-lear-smoke    # smoke window, separate output files

Method
------
* Direct multi-horizon: one lasso per (block, local slot), 76 models
  (:func:`v2g.forecast.lear_features.model_keys`). Features: today's known
  slots, full days d-1, d-2, d-3, d-6, d-7, day-of-week dummies
  (:mod:`v2g.forecast.lear_features`).
* Training set for an issue on date d: the most recent W past issues d' whose
  targets are all known at d's cutoff, i.e. d' <= d - 2 (d'+1 ends before the
  cutoff), for each W in ``calibration_windows_days``. Rows with NaN features
  are dropped; rows with a NaN target are dropped per model.
* Variance-stabilising transform (Uniejewski, Weron & Ziel 2018), fitted on the
  training window only: z = (p - median) / MAD, MAD scaled by 1/0.6745, then
  asinh(z). Applied to price features and targets; predictions are inverted
  with p = median + MAD * sinh(y). Features are then standardised with the
  training mean/std (constant columns become zero).
* Lambda: ``LassoLarsIC(criterion)`` then refit ``Lasso(alpha)`` with a high
  max_iter (as epftoolbox). With W = 56 or 84 we have n <= p + 1 (~266
  features) and recent scikit-learn refuses to estimate the noise variance
  (epftoolbox issue #10). We then pass ``noise_variance`` = the minimum
  leave-one-out mean squared error of a ``RidgeCV`` on the same window. That
  is an honest, well-conditioned estimate (ridge is closed form, LOO needs no
  extra fits) and keeps the AIC/BIC selection consistent across windows,
  whereas ``LassoCV`` with ``TimeSeriesSplit`` on 56 rows leaves folds of ~9
  samples and is several times slower. ``lambda_selection: cv`` switches every
  window to ``LassoCV(TimeSeriesSplit(5))`` instead.
* Forecasts from all windows are averaged in price space -> model ``lear``.
  With ``write_component_models`` the per-window models ``lear_wNN`` are
  written as well.
* Exogenous (raw-scale) features are clipped at prediction time to the
  z-score range seen in the training window: a linear model must not
  extrapolate an input far outside its support, and with the asinh
  back-transform an out-of-range solar or demand value turned into forecasts
  of 10^6 £/MWh (dawn/dusk solar slots in spring, after a winter-only
  window). Price features are already compressed by asinh and are not
  clipped, so prices-only ``lear`` is unaffected. Clipped counts are logged.
* Recalibration every ``recalibrate_every_days``; in between the last fit
  (including its transform and scaler) is reused but each day's features are
  rebuilt. A NaN test feature (long gap in a past day) is replaced by the
  training mean (zero after standardisation) and logged.
* Quantiles: the shared empirical-residual method
  (:mod:`v2g.forecast.quantiles`) on lear's own p50 history, with the usual
  warm-up before the window; only in-window issues are written.

Diagnostics: ``artefacts/forecasts/lear_diagnostics.parquet`` with one row per
fit x window x (block, slot): alpha, non-zero coefficients, training rows; and
``lear_top_features.csv`` with the 15 most frequently selected features per
block over the run (selection rate = share of fits with a non-zero
coefficient). Rows are recorded on recalibration days only.
"""

from __future__ import annotations

import argparse
import logging
import time
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from joblib import Parallel, delayed
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import Lasso, LassoCV, LassoLarsIC, RidgeCV
from sklearn.model_selection import TimeSeriesSplit

from shared.schema import ForecastConfig
from v2g.forecast.config import (
    DEFAULT_ASSUMPTIONS_PATH,
    WINDOWS,
    load_forecast_config,
    window_dates,
)
from v2g.forecast.io import (
    CONTRACT_COLUMNS,
    DEFAULT_FORECAST_PATH,
    DEFAULT_PRICES_PATH,
    write_forecasts,
)
from v2g.forecast.lear_data import LearData, load_lear_data, slot_of
from v2g.forecast.lear_features import (
    PAST_DAYS,
    build_features,
    feature_names,
    last_known_slot,
    model_keys,
    price_feature_mask,
    training_targets,
)
from v2g.forecast.quantiles import add_quantiles, finalise_window, warmup_start
from v2g.forecast.timing import issue_times, target_times, to_date, tzinfo

log = logging.getLogger(__name__)

MODEL_NAME = "lear"
MAD_SCALE = 0.6745
MIN_TRAIN_ROWS = 10
ALPHA_FLOOR = 1e-6
RIDGE_ALPHAS = np.logspace(-2, 3, 12)
DEFAULT_DIAGNOSTICS_PATH = Path("artefacts/forecasts/lear_diagnostics.parquet")
DEFAULT_TOP_FEATURES_PATH = Path("artefacts/forecasts/lear_top_features.csv")
DEFAULT_FEATURE_GROUPS_PATH = Path("artefacts/forecasts/lear_feature_groups.csv")
TOP_N_FEATURES = 15


# -------------------------------------------------------------------- transform


@dataclass(frozen=True)
class PriceTransform:
    """Median/MAD standardisation followed by asinh; fitted on training prices only."""

    med: float
    mad: float

    @classmethod
    def fit(cls, values) -> PriceTransform:
        v = np.asarray(values, dtype="float64").ravel()
        v = v[np.isfinite(v)]
        if v.size == 0:
            raise ValueError("cannot fit the price transform on an empty window")
        med = float(np.median(v))
        mad = float(np.median(np.abs(v - med)) / MAD_SCALE)
        if not mad > 0:
            mad = float(np.std(v)) or 1.0
        return cls(med=med, mad=mad)

    def forward(self, prices):
        return np.arcsinh((np.asarray(prices, dtype="float64") - self.med) / self.mad)

    def inverse(self, transformed):
        return self.med + self.mad * np.sinh(np.asarray(transformed, dtype="float64"))


# ------------------------------------------------------------------------ fitting


def select_and_fit(
    X: np.ndarray, y: np.ndarray, criterion: str
) -> tuple[np.ndarray, float, float, int]:
    """Choose alpha (AIC/BIC via LassoLarsIC, or CV) and refit a Lasso at that alpha."""
    n, p = X.shape
    if criterion == "cv":
        alpha = float(
            LassoCV(cv=TimeSeriesSplit(n_splits=5), max_iter=50_000).fit(X, y).alpha_
        )
    else:
        kwargs = {}
        if (
            n <= p + 1
        ):  # noise variance cannot be estimated from OLS residuals: use ridge LOO error
            ridge = RidgeCV(alphas=RIDGE_ALPHAS, store_cv_results=True).fit(X, y)
            kwargs["noise_variance"] = max(
                float(ridge.cv_results_.mean(axis=0).min()), 1e-12
            )
        alpha = float(LassoLarsIC(criterion=criterion, **kwargs).fit(X, y).alpha_)
    alpha = max(alpha, ALPHA_FLOOR)
    lasso = Lasso(alpha=alpha, max_iter=50_000).fit(X, y)
    return (
        lasso.coef_,
        float(lasso.intercept_),
        alpha,
        int(np.count_nonzero(lasso.coef_)),
    )


def _fit_columns(
    X: np.ndarray, Y: np.ndarray, columns: np.ndarray, criterion: str
) -> list[tuple]:
    """Fit one model per target column (runs in a worker)."""
    out = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        for j in columns:
            y = Y[:, j]
            ok = np.isfinite(y)
            if ok.sum() < MIN_TRAIN_ROWS:
                raise ValueError(
                    f"only {int(ok.sum())} usable training rows for model column {j}"
                )
            coef, intercept, alpha, nnz = select_and_fit(X[ok], y[ok], criterion)
            out.append((int(j), coef, intercept, alpha, nnz, int(ok.sum())))
    return out


@dataclass
class WindowFit:
    window_days: int
    fit_date: date
    transform: PriceTransform
    feature_mean: np.ndarray
    feature_std: np.ndarray
    price_mask: np.ndarray
    coef: np.ndarray  # (n_models, n_features)
    intercept: np.ndarray
    alpha: np.ndarray
    nnz: np.ndarray
    n_train: np.ndarray
    clip_lo: np.ndarray | None = (
        None  # per-feature z-score bounds applied at prediction (exog only)
    )
    clip_hi: np.ndarray | None = None

    def standardise(self, features_raw: np.ndarray) -> tuple[np.ndarray, int]:
        """Transform + z-score one raw feature vector; returns (z, number of clipped features)."""
        x = np.array(features_raw, dtype="float64")
        x[self.price_mask] = self.transform.forward(x[self.price_mask])
        z = (x - self.feature_mean) / self.feature_std
        z[~np.isfinite(z)] = 0.0  # unknown feature -> training mean
        clipped = 0
        if self.clip_lo is not None:
            out_of_range = (z < self.clip_lo) | (z > self.clip_hi)
            clipped = int(out_of_range.sum())
            z = np.clip(z, self.clip_lo, self.clip_hi)
        return z, clipped

    def predict(self, features_raw: np.ndarray) -> np.ndarray:
        """£/MWh prediction for every model key from one raw feature vector."""
        z, _ = self.standardise(features_raw)
        return self.transform.inverse(self.coef @ z + self.intercept)


# ------------------------------------------------------------------- forecaster


class LearForecaster:
    """Builds features, fits the per-(block, slot) lassos and predicts issues."""

    def __init__(
        self,
        data: LearData,
        cfg: ForecastConfig,
        windows: list[int] | None = None,
        n_jobs: int | None = None,
        lambda_selection: str | None = None,
        model_name: str = MODEL_NAME,
        exog=None,
    ):
        """``exog``: an ExogVintages switches exogenous features on; None means prices only."""
        self.data = data
        self.cfg = cfg
        self.model_name = model_name
        self.exog = exog
        self.windows = sorted(windows or cfg.lear.calibration_windows_days)
        self.n_jobs = cfg.lear.n_jobs if n_jobs is None else n_jobs
        self.criterion = (
            lambda_selection
            or cfg.lear.lambda_selection_by_model.get(model_name)
            or cfg.lear.lambda_selection
        )
        if self.criterion not in ("aic", "bic", "cv"):
            raise ValueError(
                f"lambda_selection must be aic, bic or cv, got {self.criterion!r}"
            )
        self.keys = model_keys(cfg)
        self.key_index = {k: i for i, k in enumerate(self.keys)}
        self.names = feature_names(
            cfg, exog.feature_names if exog is not None else None
        )
        self.price_mask = price_feature_mask(self.names)
        self._features: dict[date, np.ndarray] = {}
        self._targets: dict[date, np.ndarray] = {}

    def issue_time(self, d: date) -> pd.Timestamp:
        return issue_times(d, d, self.cfg)[0]

    def features(self, d: date) -> np.ndarray:
        if d not in self._features:
            vector, names = build_features(
                self.issue_time(d),
                self.data.day_matrix,
                self.data.prices_raw,
                self.cfg,
                exog=self.exog,
            )
            if names != self.names:
                raise RuntimeError("feature layout changed between issues")
            self._features[d] = vector
        return self._features[d]

    def targets(self, d: date) -> np.ndarray:
        if d not in self._targets:
            self._targets[d] = training_targets(self.data.day_matrix, d, self.cfg)
        return self._targets[d]

    @staticmethod
    def training_dates(d: date, window_days: int) -> list[date]:
        """The W most recent issue dates whose targets are all known at d's cutoff: d-W-1 .. d-2."""
        last = d - timedelta(days=2)
        return [last - timedelta(days=window_days - 1 - i) for i in range(window_days)]

    def _transform_window(self, first_training_date: date, d: date) -> PriceTransform:
        idx = self.data.day_matrix.index
        mask = (idx >= first_training_date - timedelta(days=7)) & (
            idx <= d - timedelta(days=1)
        )
        return PriceTransform.fit(self.data.day_matrix.loc[mask].to_numpy())

    def fit(self, d: date) -> list[WindowFit]:
        widest = self.training_dates(d, max(self.windows))
        X_all = np.vstack([self.features(t) for t in widest])
        Y_all = np.vstack([self.targets(t) for t in widest])

        prepared = []
        for window in self.windows:
            X, Y, dates = X_all[-window:], Y_all[-window:], widest[-window:]
            ok = np.isfinite(X).all(axis=1)
            if ok.sum() < MIN_TRAIN_ROWS:
                raise ValueError(
                    f"window {window}: only {int(ok.sum())} training rows with complete features before {d}"
                )
            X, Y = X[ok], Y[ok]
            transform = self._transform_window(dates[0], d)
            Xt = X.copy()
            Xt[:, self.price_mask] = transform.forward(Xt[:, self.price_mask])
            Yt = transform.forward(Y)
            mean = Xt.mean(axis=0)
            std = Xt.std(axis=0)
            constant = std <= 1e-12
            std = np.where(constant, 1.0, std)
            Xs = (Xt - mean) / std
            Xs[:, constant] = 0.0
            prepared.append((window, transform, mean, std, Xs, Yt))

        n_chunks = max(
            1, min(len(self.keys), (abs(self.n_jobs) if self.n_jobs != 0 else 1) * 2)
        )
        chunks = np.array_split(np.arange(len(self.keys)), n_chunks)
        per_window = len(chunks)
        order = list(range(len(prepared)))[
            ::-1
        ]  # heaviest windows first for load balance
        tasks = [
            delayed(_fit_columns)(prepared[w][4], prepared[w][5], chunk, self.criterion)
            for w in order
            for chunk in chunks
        ]
        results = Parallel(n_jobs=self.n_jobs)(tasks)
        results_by_window = {
            w: results[i * per_window : (i + 1) * per_window]
            for i, w in enumerate(order)
        }

        fits = []
        for w, (window, transform, mean, std, Xs, _) in enumerate(prepared):
            coef = np.zeros((len(self.keys), Xs.shape[1]))
            intercept = np.zeros(len(self.keys))
            alpha = np.zeros(len(self.keys))
            nnz = np.zeros(len(self.keys), dtype=int)
            n_train = np.zeros(len(self.keys), dtype=int)
            for chunk_result in results_by_window[w]:
                for j, c, b0, a, k, n in chunk_result:
                    coef[j], intercept[j], alpha[j], nnz[j], n_train[j] = c, b0, a, k, n
            fits.append(
                WindowFit(
                    window_days=window,
                    fit_date=d,
                    transform=transform,
                    feature_mean=mean,
                    feature_std=std,
                    price_mask=self.price_mask,
                    coef=coef,
                    intercept=intercept,
                    alpha=alpha,
                    nnz=nnz,
                    n_train=n_train,
                    clip_lo=np.where(self.price_mask, -np.inf, Xs.min(axis=0)),
                    clip_hi=np.where(self.price_mask, np.inf, Xs.max(axis=0)),
                )
            )
        return fits

    def forecast_issue(self, d: date, fits: list[WindowFit]) -> pd.DataFrame:
        """Rows for every target of the issue on ``d``: averaged p50 plus one column per window."""
        issue_utc = self.issue_time(d)
        targets = target_times(issue_utc, self.cfg)
        x = self.features(d)
        if not np.isfinite(x).all():
            log.warning(
                "%s: %d NaN features replaced by the training mean",
                d,
                int((~np.isfinite(x)).sum()),
            )
        standardised = [f.standardise(x) for f in fits]
        clipped = sum(c for _, c in standardised)
        if clipped:
            log.info(
                "%s: %d exogenous feature values clipped to the training range",
                d,
                clipped,
            )
        per_window = np.vstack(
            [
                f.transform.inverse(f.coef @ z + f.intercept)
                for f, (z, _) in zip(fits, standardised)
            ]
        )  # (n_windows, n_models)
        local = targets.tz_convert(tzinfo(self.cfg))
        blocks = np.where(local.date == d, "A", "B")
        slots = slot_of(local)
        index = [self.key_index[(b, int(s))] for b, s in zip(blocks, slots)]
        frame = pd.DataFrame(
            {
                "issue_time_utc": issue_utc,
                "target_time_utc": targets,
                "block": blocks,
                "slot": slots,
                "p50": per_window.mean(axis=0)[index],
            }
        )
        for f, row in zip(fits, per_window):
            frame[f"p50_w{f.window_days}"] = row[index]
        return frame


# --------------------------------------------------------------------------- run


@dataclass
class LearRun:
    forecasts: pd.DataFrame  # written rows (contract columns)
    points: pd.DataFrame  # all issues incl. warm-up, with block/slot and per-window p50
    diagnostics: pd.DataFrame
    top_features: pd.DataFrame
    feature_groups: pd.DataFrame
    runtime_s: float
    n_fits: int = 0
    notes: list[str] = field(default_factory=list)


class _SelectionGroups:
    """Accumulates, per block, how often lag-K same-slot features and feature groups are selected.

    'same slot' is relative to the issue date d: dmK = day d-K at the target's slot (for a Block B
    target on d+1 that is K+1 days before the target); 'today' = slot of day d, known only for
    slots up to the cutoff.
    """

    def __init__(
        self, names: list[str], keys: list[tuple[str, int]], cfg: ForecastConfig
    ):
        self.rows = {
            b: np.array([j for j, (blk, _) in enumerate(keys) if blk == b])
            for b in ("A", "B")
        }
        self.same_idx = {
            f"dm{k}": np.array([names.index(f"dm{k}_s{slot:02d}") for _, slot in keys])
            for k in PAST_DAYS
        }
        known = last_known_slot(cfg)
        self.same_idx["today"] = np.array(
            [
                names.index(f"today_s{slot:02d}") if slot <= known else -1
                for _, slot in keys
            ]
        )
        prefixes = [
            ("today", "today_"),
            *[(f"dm{k}", f"dm{k}_") for k in PAST_DAYS],
            ("dow", "dow_"),
        ]
        price_like = np.array([n.startswith(("today_", "dm", "dow_")) for n in names])
        exog_series = sorted(
            {
                n.rsplit("_d", 1)[0]
                for n, is_price in zip(names, price_like)
                if not is_price
            }
        )
        prefixes += [(f"exog:{e}", f"{e}_d") for e in exog_series]
        self.group_mask = {
            g: np.array([n.startswith(prefix) for n in names]) for g, prefix in prefixes
        }
        self.exog_mask = ~price_like
        self.same_hits = {b: dict.fromkeys(self.same_idx, 0.0) for b in ("A", "B")}
        self.same_n = {b: dict.fromkeys(self.same_idx, 0) for b in ("A", "B")}
        self.group_sum = {b: dict.fromkeys(self.group_mask, 0.0) for b in ("A", "B")}
        self.n_models = {b: 0 for b in ("A", "B")}
        self.selected_exog = {b: 0.0 for b in ("A", "B")}
        self.selected_total = {b: 0.0 for b in ("A", "B")}

    def add(self, coef: np.ndarray) -> None:
        selected = coef != 0
        for block, rows in self.rows.items():
            self.selected_total[block] += float(selected[rows].sum())
            self.selected_exog[block] += float(selected[rows][:, self.exog_mask].sum())
            for group, idx in self.same_idx.items():
                valid = rows[idx[rows] >= 0]
                self.same_hits[block][group] += float(selected[valid, idx[valid]].sum())
                self.same_n[block][group] += len(valid)
            for group, mask in self.group_mask.items():
                self.group_sum[block][group] += float(selected[rows][:, mask].sum())
            self.n_models[block] += len(rows)

    def frame(self) -> pd.DataFrame:
        out = []
        for block in ("A", "B"):
            for group in self.same_idx:
                n = self.same_n[block][group]
                out.append(
                    {
                        "block": block,
                        "group": group,
                        "metric": "same_slot_selection_rate",
                        "value": self.same_hits[block][group] / n if n else np.nan,
                    }
                )
            for group in self.group_mask:
                n = self.n_models[block]
                out.append(
                    {
                        "block": block,
                        "group": group,
                        "metric": "mean_selected_count",
                        "value": self.group_sum[block][group] / n if n else np.nan,
                    }
                )
            total = self.selected_total[block]
            for group, count in (
                ("exogenous", self.selected_exog[block]),
                ("price", total - self.selected_exog[block]),
            ):
                out.append(
                    {
                        "block": block,
                        "group": group,
                        "metric": "share_of_selected",
                        "value": count / total if total else np.nan,
                    }
                )
        return pd.DataFrame(out, columns=["block", "group", "metric", "value"])


def _top_features(
    selection: dict[str, np.ndarray], fits_per_block: dict[str, int], names: list[str]
) -> pd.DataFrame:
    rows = []
    for block, counts in selection.items():
        if fits_per_block[block] == 0:
            continue
        rate = counts / fits_per_block[block]
        order = np.argsort(-rate, kind="stable")[:TOP_N_FEATURES]
        rows += [
            {
                "block": block,
                "rank": r + 1,
                "feature": names[i],
                "selection_rate": float(rate[i]),
            }
            for r, i in enumerate(order)
        ]
    return pd.DataFrame(rows, columns=["block", "rank", "feature", "selection_rate"])


def run(
    start_date,
    end_date,
    cfg: ForecastConfig,
    *,
    prices_path: Path | str = DEFAULT_PRICES_PATH,
    out_path: Path | str = DEFAULT_FORECAST_PATH,
    recalibrate_every: int | None = None,
    windows: list[int] | None = None,
    n_jobs: int | None = None,
    lambda_selection: str | None = None,
    model_name: str = MODEL_NAME,
    exog: bool | None = None,
    exog_dir: Path | str | None = None,
    write_components: bool | None = None,
    diagnostics_path: Path | str | None = DEFAULT_DIAGNOSTICS_PATH,
    top_features_path: Path | str | None = DEFAULT_TOP_FEATURES_PATH,
    feature_groups_path: Path | str | None = DEFAULT_FEATURE_GROUPS_PATH,
) -> LearRun:
    """Forecast every issue in [start_date, end_date] with LEAR and upsert into the contract parquet."""
    t0 = time.perf_counter()
    start, end = to_date(start_date), to_date(end_date)
    every = recalibrate_every or cfg.lear.recalibrate_every_days
    components = (
        cfg.lear.write_component_models
        if write_components is None
        else write_components
    )

    data = load_lear_data(prices_path, cfg)
    use_exog = model_name in cfg.exog.enabled_for_models if exog is None else exog
    vintages = None
    if use_exog:
        from v2g.forecast.exog import load_exog

        vintages = load_exog(cfg, exog_dir) if exog_dir else load_exog(cfg)
        log.info(
            "%s: exogenous features on (%d series, %d vintage rows)",
            model_name,
            len(vintages.series),
            vintages.n_rows,
        )
    forecaster = LearForecaster(
        data,
        cfg,
        windows=windows,
        n_jobs=n_jobs,
        lambda_selection=lambda_selection,
        model_name=model_name,
        exog=vintages,
    )
    first = warmup_start(start, cfg)
    dates = [first + timedelta(days=i) for i in range((end - first).days + 1)]
    log.info(
        "%s: windows %s, lambda by %s, recalibrate every %d day(s), %d issues %s..%s (warm-up from %s), n_jobs=%s",
        model_name,
        forecaster.windows,
        forecaster.criterion,
        every,
        len(dates),
        start,
        end,
        first,
        forecaster.n_jobs,
    )

    blocks = [k[0] for k in forecaster.keys]
    groups = _SelectionGroups(forecaster.names, forecaster.keys, cfg)
    selection = {
        "A": np.zeros(len(forecaster.names)),
        "B": np.zeros(len(forecaster.names)),
    }
    fits_per_block = {"A": 0, "B": 0}
    diagnostics: list[dict] = []
    frames: list[pd.DataFrame] = []
    fits: list[WindowFit] | None = None
    fit_date: date | None = None
    n_fits = 0
    for i, d in enumerate(dates, 1):
        if fits is None or (d - fit_date).days >= every:
            fits = forecaster.fit(d)
            fit_date = d
            n_fits += 1
            for f in fits:
                for j, (block, slot) in enumerate(forecaster.keys):
                    diagnostics.append(
                        {
                            "fit_date": d,
                            "window_days": f.window_days,
                            "block": block,
                            "slot": slot,
                            "alpha": f.alpha[j],
                            "nnz": f.nnz[j],
                            "n_train": f.n_train[j],
                        }
                    )
                for block in ("A", "B"):
                    rows = [j for j, b in enumerate(blocks) if b == block]
                    selection[block] += (f.coef[rows] != 0).sum(axis=0)
                    fits_per_block[block] += len(rows)
                groups.add(f.coef)
        frames.append(forecaster.forecast_issue(d, fits))
        if i % 5 == 0 or i == len(dates):
            elapsed = time.perf_counter() - t0
            eta = elapsed / i * (len(dates) - i)
            log.info(
                "issue %d/%d (%s) done; elapsed %.0fs, ETA %.0fs",
                i,
                len(dates),
                d,
                elapsed,
                eta,
            )

    points = pd.concat(frames, ignore_index=True)
    first_in_window = issue_times(start, start, cfg)[0]
    outputs = []
    model_columns = {model_name: "p50"}
    if components:
        model_columns.update(
            {f"{model_name}_w{w}": f"p50_w{w}" for w in forecaster.windows}
        )
    for model, column in model_columns.items():
        pts = points[["issue_time_utc", "target_time_utc"]].assign(
            model=model, series=cfg.series, p50=points[column].astype("float64")
        )
        outputs.append(
            finalise_window(
                add_quantiles(pts, data.prices_raw, cfg), first_in_window, model, cfg
            )
        )
    forecasts = pd.concat(outputs, ignore_index=True)[CONTRACT_COLUMNS]
    write_forecasts(forecasts, out_path, cfg)

    diag = pd.DataFrame(diagnostics)
    top = _top_features(selection, fits_per_block, forecaster.names)
    if diagnostics_path is not None:
        Path(diagnostics_path).parent.mkdir(parents=True, exist_ok=True)
        diag.to_parquet(diagnostics_path, index=False)
    if top_features_path is not None:
        Path(top_features_path).parent.mkdir(parents=True, exist_ok=True)
        top.to_csv(top_features_path, index=False)
    feature_groups = groups.frame()
    if feature_groups_path is not None:
        Path(feature_groups_path).parent.mkdir(parents=True, exist_ok=True)
        feature_groups.to_csv(feature_groups_path, index=False)
    runtime = time.perf_counter() - t0
    log.info(
        "LEAR done: %d issues, %d recalibrations, %.1fs total (%.2fs per issue)",
        len(dates),
        n_fits,
        runtime,
        runtime / len(dates),
    )
    return LearRun(
        forecasts=forecasts,
        points=points,
        diagnostics=diag,
        top_features=top,
        feature_groups=feature_groups,
        runtime_s=runtime,
        n_fits=n_fits,
    )


def format_feature_groups(groups: pd.DataFrame) -> str:
    if groups.empty:
        return "(no fits recorded)"
    same = groups[groups["metric"] == "same_slot_selection_rate"].pivot(
        index="group", columns="block", values="value"
    )
    count = groups[groups["metric"] == "mean_selected_count"].pivot(
        index="group", columns="block", values="value"
    )
    order = ["today", *[f"dm{k}" for k in PAST_DAYS], "dow"] + sorted(
        g for g in count.index if g.startswith("exog:")
    )
    table = pd.DataFrame(
        {
            "same-slot selected A": same.reindex(order)["A"].map(
                lambda v: f"{100 * v:.0f}%" if pd.notna(v) else "-"
            ),
            "same-slot selected B": same.reindex(order)["B"].map(
                lambda v: f"{100 * v:.0f}%" if pd.notna(v) else "-"
            ),
            "mean # selected A": count.reindex(order)["A"].map(lambda v: f"{v:.1f}"),
            "mean # selected B": count.reindex(order)["B"].map(lambda v: f"{v:.1f}"),
        }
    )
    table.index.name = "feature group"
    share = groups[groups["metric"] == "share_of_selected"].pivot(
        index="group", columns="block", values="value"
    )
    share_txt = " | ".join(
        f"{g}: {100 * share.loc[g, 'A']:.0f}% (A), {100 * share.loc[g, 'B']:.0f}% (B)"
        for g in ("exogenous", "price")
        if g in share.index and share.loc[g].notna().all()
    )
    return table.to_string() + (
        f"\n  share of selected coefficients: {share_txt}" if share_txt else ""
    )


def format_top_features(top: pd.DataFrame) -> str:
    if top.empty:
        return "(no fits recorded)"
    wide = top.pivot(index="rank", columns="block", values="feature")
    rates = top.pivot(index="rank", columns="block", values="selection_rate")
    out = pd.DataFrame(
        {
            f"block {b}": wide[b] + rates[b].map(lambda r: f" ({100 * r:.0f}%)")
            for b in wide.columns
        }
    )
    return out.to_string()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m v2g.forecast.lear",
        description="LEAR price forecasts -> contract parquet",
    )
    p.add_argument("--window", choices=WINDOWS, default="smoke")
    p.add_argument("--start", help="first local issue date (overrides the window)")
    p.add_argument(
        "--end", help="last local issue date, inclusive (overrides the window)"
    )
    p.add_argument(
        "--recalibrate-every",
        type=int,
        help="days between refits (default from config)",
    )
    p.add_argument(
        "--windows",
        help="comma-separated calibration windows in days (default from config)",
    )
    p.add_argument("--n-jobs", type=int, help="joblib workers (default from config)")
    p.add_argument(
        "--lambda-selection",
        choices=["aic", "bic", "cv"],
        help="override the config criterion",
    )
    p.add_argument(
        "--model-name",
        default=MODEL_NAME,
        help="model name to write (default lear; lear_x enables exog via config)",
    )
    p.add_argument(
        "--exog",
        dest="exog",
        action="store_true",
        default=None,
        help="force exogenous features on",
    )
    p.add_argument(
        "--no-exog",
        dest="exog",
        action="store_false",
        help="force exogenous features off",
    )
    p.add_argument(
        "--write-components",
        action="store_true",
        help="also write lear_wNN per-window models",
    )
    p.add_argument("--prices", default=str(DEFAULT_PRICES_PATH))
    p.add_argument("--out", default=str(DEFAULT_FORECAST_PATH))
    p.add_argument("--diagnostics", default=str(DEFAULT_DIAGNOSTICS_PATH))
    p.add_argument("--top-features", default=str(DEFAULT_TOP_FEATURES_PATH))
    p.add_argument("--feature-groups", default=str(DEFAULT_FEATURE_GROUPS_PATH))
    p.add_argument("--config", default=str(DEFAULT_ASSUMPTIONS_PATH))
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_forecast_config(args.config)
    start, end = window_dates(cfg, args.window)
    start = to_date(args.start) if args.start else start
    end = to_date(args.end) if args.end else end
    windows = [int(w) for w in args.windows.split(",")] if args.windows else None
    result = run(
        start,
        end,
        cfg,
        prices_path=args.prices,
        out_path=args.out,
        recalibrate_every=args.recalibrate_every,
        windows=windows,
        n_jobs=args.n_jobs,
        lambda_selection=args.lambda_selection,
        model_name=args.model_name,
        exog=args.exog,
        write_components=True if args.write_components else None,
        diagnostics_path=args.diagnostics,
        top_features_path=args.top_features,
        feature_groups_path=args.feature_groups,
    )
    counts = result.forecasts.groupby("model").agg(
        issues=("issue_time_utc", "nunique"), rows=("target_time_utc", "size")
    )
    print(counts.to_string())
    print(
        f"\nTop selected features (selection rate over {result.n_fits} recalibrations x windows x slots):"
    )
    print(format_top_features(result.top_features))
    print(
        "\nSelection by feature group (same slot = the target's slot on that day; dmK = day d-K):"
    )
    print(format_feature_groups(result.feature_groups))
    print(f"\nTotal runtime: {result.runtime_s:.1f}s; written to {args.out}")


if __name__ == "__main__":
    main()
