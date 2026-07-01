"""Reliability diagrams.

A reliability curve bins forecasts and plots mean predicted vs mean observed frequency; a
well-calibrated model lies on the diagonal. We also draw the forecast histogram (sharpness).
Plotting uses a non-interactive backend so it works headless.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


def reliability_curve(probs, outcomes, n_bins: int = 10):
    """Return (mean_pred, mean_obs, counts) per occupied bin."""
    p = np.asarray(probs, dtype=float)
    o = np.asarray(outcomes, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1], right=False), 0, n_bins - 1)
    mean_pred, mean_obs, counts = [], [], []
    for k in range(n_bins):
        mask = idx == k
        if not mask.any():
            continue
        mean_pred.append(float(p[mask].mean()))
        mean_obs.append(float(o[mask].mean()))
        counts.append(int(mask.sum()))
    return np.array(mean_pred), np.array(mean_obs), np.array(counts)


def plot_reliability(probs, outcomes, path: str | Path, title: str = "Reliability", n_bins: int = 10):
    """Save a reliability diagram + forecast histogram to ``path`` (PNG)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    mp, mo, counts = reliability_curve(probs, outcomes, n_bins)
    fig, (ax, axh) = plt.subplots(
        2, 1, figsize=(5, 6), gridspec_kw={"height_ratios": [3, 1]}
    )
    ax.plot([0, 1], [0, 1], "k--", lw=1, label="perfect")
    ax.plot(mp, mo, "o-", color="#1f77b4", label="model")
    ax.set_xlabel("mean predicted probability")
    ax.set_ylabel("observed frequency")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_title(title)
    ax.legend(loc="upper left")

    axh.hist(np.asarray(probs, float), bins=n_bins, range=(0, 1), color="#888")
    axh.set_xlabel("forecast probability")
    axh.set_ylabel("count")

    fig.tight_layout()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
