"""GrCF counterfactual explanations for the ENTWINE intelligence layer.

This module contains only the research architecture needed to generate a
counterfactual sequence: the mentor's LSTM autoencoder, LSTM denoiser,
Granger graph, physics/causal/smoothness penalties, and DDPM-warm-started
Adam optimizer. Evaluation baselines, plotting, Kaggle paths, and fairness
tables are intentionally excluded.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Final

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as functional
import torch.optim as optim
from scipy import stats
from sklearn.preprocessing import MinMaxScaler

LOGGER: Final[logging.Logger] = logging.getLogger("models.grcf_explainer")
SEED: Final[int] = 42
SEQ_LEN: Final[int] = 16
BATCH: Final[int] = 64
EPOCHS_AE: Final[int] = 50
EPOCHS_DIFF: Final[int] = 40
LATENT_DIM: Final[int] = 32
DIFF_STEPS: Final[int] = 100
LEARNING_RATE: Final[float] = 1e-3
CF_ITER: Final[int] = 150
CF_LR: Final[float] = 0.05
LAMBDA_RECON: Final[float] = 0.2
LAMBDA_PHYSICS: Final[float] = 0.2
LAMBDA_CAUSAL: Final[float] = 0.2
LAMBDA_SMOOTH: Final[float] = 0.1
ENERGY_TOL: Final[float] = 0.15
POWER_IDX: Final[int] = 0
DEVICE: Final[torch.device] = torch.device(
    "cuda" if torch.cuda.is_available() else "cpu"
)

FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "real_power_kw",
    "is_gap",
    "is_holiday",
    "is_weekend",
    "is_workhour",
    "apparent_power_kva",
    "current_avg_a",
    "frequency_hz",
    "power_factor_pct",
    "voltage_ll_avg_v",
)
CAUSAL_COLUMNS: Final[tuple[str, ...]] = (
    "real_power_kw",
    "current_avg_a",
    "frequency_hz",
    "power_factor_pct",
    "voltage_ll_avg_v",
)


def _seed_torch() -> None:
    """Set deterministic seeds for reproducible temporary model fitting."""
    torch.manual_seed(SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(SEED)


class LSTMAutoEncoder(nn.Module):
    """Mentor's two-layer LSTM sequence autoencoder architecture."""

    def __init__(self, n_feat: int, seq_len: int, latent_dim: int) -> None:
        """Initialize the encoder, repeated latent decoder, and output layer."""
        super().__init__()
        self.encoder = nn.LSTM(
            n_feat,
            latent_dim,
            num_layers=2,
            batch_first=True,
            dropout=0.2,
        )
        self.decoder = nn.LSTM(
            latent_dim,
            latent_dim,
            num_layers=2,
            batch_first=True,
            dropout=0.2,
        )
        self.fc_out = nn.Linear(latent_dim, n_feat)
        self.seq_len = seq_len

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Reconstruct a batch of multivariate sequences."""
        _, (hidden, cell) = self.encoder(x)
        latent = hidden[-1].unsqueeze(1).repeat(1, self.seq_len, 1)
        output, _ = self.decoder(latent, (hidden, cell))
        return self.fc_out(output)


class LSTMDenoiser(nn.Module):
    """Mentor's timestep-conditioned LSTM DDPM noise-prediction architecture."""

    def __init__(
        self,
        n_feat: int,
        hidden: int = 64,
        t_emb_dim: int = 16,
    ) -> None:
        """Initialize timestep embedding, recurrent denoiser, and output layer."""
        super().__init__()
        self.t_emb = nn.Embedding(DIFF_STEPS + 1, t_emb_dim)
        self.fc_in = nn.Linear(n_feat + t_emb_dim, hidden)
        self.lstm = nn.LSTM(
            hidden,
            hidden,
            num_layers=2,
            batch_first=True,
            dropout=0.1,
        )
        self.fc_out = nn.Linear(hidden, n_feat)

    def forward(self, x_t: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
        """Predict the Gaussian noise added at diffusion timestep ``t``."""
        time_embedding = self.t_emb(t).unsqueeze(1).expand(-1, x_t.size(1), -1)
        hidden = functional.relu(self.fc_in(torch.cat([x_t, time_embedding], dim=-1)))
        output, _ = self.lstm(hidden)
        return self.fc_out(output)


_BETAS = torch.linspace(1e-4, 0.02, DIFF_STEPS, device=DEVICE)
_ALPHAS = 1.0 - _BETAS
_ALPHA_BAR = torch.cumprod(_ALPHAS, dim=0)
_SQRT_ALPHA_BAR = _ALPHA_BAR.sqrt()
_SQRT_ONE_MINUS_ALPHA_BAR = (1.0 - _ALPHA_BAR).sqrt()


def q_sample(
    clean_sequences: torch.Tensor,
    timestep: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Add timestep-specific Gaussian noise to clean sequence tensors."""
    noise = torch.randn_like(clean_sequences)
    noisy = (
        _SQRT_ALPHA_BAR[timestep].view(-1, 1, 1) * clean_sequences
        + _SQRT_ONE_MINUS_ALPHA_BAR[timestep].view(-1, 1, 1) * noise
    )
    return noisy, noise


@torch.no_grad()
def ddpm_sample(model: LSTMDenoiser, shape: tuple[int, int, int]) -> torch.Tensor:
    """Draw a normal-manifold warm-start sample with DDPM reverse diffusion."""
    sample = torch.randn(*shape, device=DEVICE)
    for timestep in reversed(range(DIFF_STEPS)):
        time = torch.full(
            (shape[0],), timestep, device=DEVICE, dtype=torch.long
        )
        predicted_noise = model(sample, time)
        alpha_bar = _ALPHA_BAR[timestep]
        previous_alpha_bar = (
            _ALPHA_BAR[timestep - 1]
            if timestep > 0
            else torch.tensor(1.0, device=DEVICE)
        )
        predicted_clean = (
            (sample - _SQRT_ONE_MINUS_ALPHA_BAR[timestep] * predicted_noise)
            / _SQRT_ALPHA_BAR[timestep]
        ).clamp(0, 1)
        mean = (
            previous_alpha_bar.sqrt() * _BETAS[timestep] / (1 - alpha_bar)
        ) * predicted_clean + (
            _ALPHAS[timestep].sqrt()
            * (1 - previous_alpha_bar)
            / (1 - alpha_bar)
        ) * sample
        if timestep > 0:
            variance = (
                _BETAS[timestep]
                * (1 - previous_alpha_bar)
                / (1 - alpha_bar)
            ).sqrt()
            sample = mean + variance * torch.randn_like(sample)
        else:
            sample = mean
    return sample.clamp(0, 1)


@torch.no_grad()
def random_normal_sample(shape: tuple[int, int, int]) -> torch.Tensor:
    """Draw a uniform fallback initialization when DDPM warm-start is disabled."""
    return torch.rand(*shape, device=DEVICE)


def granger_causal_graph(
    dataframe: pd.DataFrame,
    columns: Sequence[str],
    max_lag: int = 4,
    alpha: float = 0.05,
) -> np.ndarray:
    """Estimate a directed Granger-causality adjacency matrix.

    ``A[i, j] == 1`` means column ``i`` provides statistically significant
    lagged information for predicting column ``j`` beyond ``j``'s own history.
    """
    selected = dataframe.loc[:, list(columns)].dropna().to_numpy(dtype=float)
    feature_count = len(columns)
    adjacency = np.zeros((feature_count, feature_count), dtype=np.float32)
    if len(selected) <= 2 * max_lag + 1:
        return adjacency

    for cause in range(feature_count):
        for effect in range(feature_count):
            if cause == effect:
                continue
            target = selected[max_lag:, effect]
            restricted = np.column_stack(
                [selected[max_lag - lag : -lag, effect] for lag in range(1, max_lag + 1)]
            )
            unrestricted = np.column_stack(
                [
                    restricted,
                    *[
                        selected[max_lag - lag : -lag, cause]
                        for lag in range(1, max_lag + 1)
                    ],
                ]
            )
            try:
                restricted_beta = np.linalg.lstsq(
                    restricted, target, rcond=None
                )[0]
                unrestricted_beta = np.linalg.lstsq(
                    unrestricted, target, rcond=None
                )[0]
                restricted_rss = np.sum(
                    (target - restricted @ restricted_beta) ** 2
                )
                unrestricted_rss = np.sum(
                    (target - unrestricted @ unrestricted_beta) ** 2
                )
                degrees = len(target) - 2 * max_lag
                if degrees <= 0 or unrestricted_rss < 1e-12:
                    continue
                f_statistic = (
                    (restricted_rss - unrestricted_rss) / max_lag
                ) / (unrestricted_rss / degrees)
                p_value = 1.0 - stats.f.cdf(f_statistic, max_lag, degrees)
                if p_value < alpha:
                    adjacency[cause, effect] = 1.0
            except (FloatingPointError, ValueError, np.linalg.LinAlgError):
                continue
    return adjacency


def physics_energy_penalty(
    counterfactual: torch.Tensor,
    anomaly: torch.Tensor,
) -> torch.Tensor:
    """Penalize counterfactual windows whose total power changes excessively."""
    counterfactual_power = counterfactual[:, :, POWER_IDX].sum(1)
    anomaly_power = anomaly[:, :, POWER_IDX].sum(1).detach()
    deviation = (counterfactual_power - anomaly_power).abs() / (
        anomaly_power.abs() + 1e-8
    )
    return functional.relu(deviation - ENERGY_TOL).pow(2).mean()


def granger_constraint_penalty(
    counterfactual: torch.Tensor,
    anomaly: torch.Tensor,
    adjacency: np.ndarray,
    feature_indices: Mapping[str, int],
) -> torch.Tensor:
    """Penalize effects changing more than their Granger causes."""
    penalty = torch.tensor(0.0, device=DEVICE)
    count = 0
    names = list(feature_indices.keys())
    for cause_name, cause_index in feature_indices.items():
        for effect_name, effect_index in feature_indices.items():
            cause_position = names.index(cause_name)
            effect_position = names.index(effect_name)
            if adjacency[cause_position, effect_position] == 1:
                cause_delta = (
                    counterfactual[:, :, cause_index] - anomaly[:, :, cause_index]
                ).abs().mean()
                effect_delta = (
                    counterfactual[:, :, effect_index] - anomaly[:, :, effect_index]
                ).abs().mean()
                penalty = penalty + functional.relu(effect_delta - cause_delta).pow(2)
                count += 1
    return penalty / max(count, 1)


def smoothness_penalty(counterfactual: torch.Tensor) -> torch.Tensor:
    """Penalize abrupt changes between adjacent counterfactual timesteps."""
    return (counterfactual[:, 1:, :] - counterfactual[:, :-1, :]).pow(2).mean()


def generate_cf(
    anomaly_seq: np.ndarray,
    denoiser: LSTMDenoiser,
    causal_matrix: np.ndarray,
    causal_feat_idx: Mapping[str, int],
    use_n1: bool = True,
    use_n2: bool = True,
    use_n3: bool = True,
    n_iter: int = CF_ITER,
    lr: float = CF_LR,
) -> tuple[np.ndarray, dict[str, list[float]]]:
    """Generate a GrCF counterfactual with DDPM initialization and Adam.

    The DDPM sample provides the warm start when ``use_n3`` is enabled. Adam
    then minimizes proximity, Granger, physics, and smoothness penalties.
    """
    feature_count = int(anomaly_seq.shape[1])
    anomaly_tensor = torch.as_tensor(
        anomaly_seq, dtype=torch.float32, device=DEVICE
    ).unsqueeze(0)
    with torch.no_grad():
        if use_n3:
            initial = ddpm_sample(denoiser, (1, SEQ_LEN, feature_count))
        else:
            initial = random_normal_sample((1, SEQ_LEN, feature_count))
    counterfactual = initial.clone().requires_grad_(True)
    optimizer = optim.Adam([counterfactual], lr=lr)
    losses: dict[str, list[float]] = {"total": [], "phys": [], "granger": []}
    physics_weight = LAMBDA_PHYSICS if use_n2 else 0.0
    causal_weight = LAMBDA_CAUSAL if use_n1 else 0.0

    for _ in range(n_iter):
        clipped = counterfactual.clamp(0, 1)
        reconstruction_loss = functional.mse_loss(clipped, anomaly_tensor)
        physics_loss = (
            physics_energy_penalty(clipped, anomaly_tensor)
            if use_n2
            else torch.tensor(0.0, device=DEVICE)
        )
        causal_loss = (
            granger_constraint_penalty(
                clipped, anomaly_tensor, causal_matrix, causal_feat_idx
            )
            if use_n1
            else torch.tensor(0.0, device=DEVICE)
        )
        smooth_loss = smoothness_penalty(clipped)
        total = (
            LAMBDA_RECON * reconstruction_loss
            + physics_weight * physics_loss
            + causal_weight * causal_loss
            + LAMBDA_SMOOTH * smooth_loss
        )
        optimizer.zero_grad()
        total.backward()
        optimizer.step()
        losses["total"].append(float(total.item()))
        losses["phys"].append(float(physics_loss.item()))
        losses["granger"].append(float(causal_loss.item()))

    return counterfactual.clamp(0, 1).detach().squeeze(0).cpu().numpy(), losses


def _prepare_feature_frame(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Create the mentor feature columns without using anomaly labels."""
    frame = dataframe.copy()
    if "time" in frame.columns and not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.pop("time"), utc=True)
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("DataFrame must have a DatetimeIndex or a 'time' column.")
    frame.index = (
        frame.index.tz_localize("UTC")
        if frame.index.tz is None
        else frame.index.tz_convert("UTC")
    )
    frame.sort_index(inplace=True)

    if "real_power_kw" not in frame.columns:
        raise ValueError("DataFrame must contain 'real_power_kw'.")
    target = pd.to_numeric(frame["real_power_kw"], errors="coerce")
    frame["is_gap"] = target.isna().astype(float)
    frame["real_power_kw"] = target
    frame["is_weekend"] = (frame.index.weekday >= 5).astype(float)
    frame["is_workhour"] = (
        (frame.index.weekday < 5)
        & (frame.index.hour >= 8)
        & (frame.index.hour < 18)
    ).astype(float)
    frame["is_holiday"] = pd.to_numeric(
        frame.get("is_holiday", pd.Series(0.0, index=frame.index)),
        errors="coerce",
    ).fillna(0.0)
    for column in FEATURE_COLUMNS:
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _make_sequences(
    values: np.ndarray,
    normal_mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Construct overlapping windows and identify windows wholly in normal data."""
    if len(values) < SEQ_LEN:
        raise ValueError(f"At least {SEQ_LEN} rows are required.")
    sequences = np.stack(
        [values[start : start + SEQ_LEN] for start in range(len(values) - SEQ_LEN + 1)]
    )
    sequence_mask = np.array(
        [
            bool(normal_mask[start : start + SEQ_LEN].all())
            for start in range(len(sequences))
        ],
        dtype=bool,
    )
    return sequences, sequence_mask


def _fit_autoencoder(normal_sequences: np.ndarray) -> LSTMAutoEncoder:
    """Train the mentor autoencoder only on normal sequences."""
    model = LSTMAutoEncoder(normal_sequences.shape[2], SEQ_LEN, LATENT_DIM).to(DEVICE)
    optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE, weight_decay=1e-5)
    criterion = nn.MSELoss()
    tensor = torch.as_tensor(normal_sequences, dtype=torch.float32, device=DEVICE)
    model.train()
    for _ in range(EPOCHS_AE):
        optimizer.zero_grad()
        loss = criterion(model(tensor), tensor)
        loss.backward()
        optimizer.step()
    return model


def _fit_denoiser(normal_sequences: np.ndarray) -> LSTMDenoiser:
    """Train the mentor DDPM denoiser only on normal sequences."""
    model = LSTMDenoiser(normal_sequences.shape[2]).to(DEVICE)
    optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE)
    criterion = nn.MSELoss()
    tensor = torch.as_tensor(normal_sequences, dtype=torch.float32, device=DEVICE)
    model.train()
    for _ in range(EPOCHS_DIFF):
        timestep = torch.randint(
            0, DIFF_STEPS, (len(tensor),), device=DEVICE, dtype=torch.long
        )
        noisy, noise = q_sample(tensor, timestep)
        optimizer.zero_grad()
        loss = criterion(model(noisy, timestep), noise)
        loss.backward()
        optimizer.step()
    return model


def explain_anomaly(
    dataframe: pd.DataFrame,
    normal_mask: np.ndarray,
    anomaly_idx: int,
) -> np.ndarray:
    """Generate a counterfactual for one overlapping sequence.

    Args:
        dataframe: Time-indexed telemetry DataFrame with model sensor columns.
        normal_mask: Boolean row-level mask identifying trusted normal records.
        anomaly_idx: Zero-based index of the target overlapping sequence.

    Returns:
        Counterfactual sequence in the original feature units with shape
        ``(SEQ_LEN, len(FEATURE_COLUMNS))``.

    Raises:
        ValueError: If the input mask, target sequence, or normal training set
            is invalid.
    """
    if len(normal_mask) != len(dataframe):
        raise ValueError("normal_mask must have one value per DataFrame row.")
    if normal_mask.dtype != bool:
        normal_mask = normal_mask.astype(bool)

    frame = _prepare_feature_frame(dataframe)
    frame_values = frame.loc[:, list(FEATURE_COLUMNS)].to_numpy(dtype=float)
    frame_values = np.nan_to_num(frame_values, nan=0.0, posinf=0.0, neginf=0.0)
    sequences_raw, sequence_normal_mask = _make_sequences(frame_values, normal_mask)
    if anomaly_idx < 0 or anomaly_idx >= len(sequences_raw):
        raise ValueError("anomaly_idx must identify an existing sequence.")
    normal_sequences_raw = sequences_raw[sequence_normal_mask]
    if len(normal_sequences_raw) == 0:
        raise ValueError("At least one fully normal sequence is required.")

    # R2 leakage fix: fit only on normal sequences, never on the target window.
    scaler = MinMaxScaler()
    scaler.fit(normal_sequences_raw.reshape(-1, sequences_raw.shape[2]))
    scaled_sequences = scaler.transform(
        sequences_raw.reshape(-1, sequences_raw.shape[2])
    ).reshape(sequences_raw.shape).astype(np.float32)
    scaled_normal = scaled_sequences[sequence_normal_mask]

    _seed_torch()
    _fit_autoencoder(scaled_normal)
    denoiser = _fit_denoiser(scaled_normal)
    causal_columns = [column for column in CAUSAL_COLUMNS if column in frame.columns]
    causal_matrix = granger_causal_graph(frame, causal_columns)
    causal_indices = {
        column: list(FEATURE_COLUMNS).index(column)
        for column in causal_columns
    }
    counterfactual_scaled, _ = generate_cf(
        scaled_sequences[anomaly_idx],
        denoiser,
        causal_matrix,
        causal_indices,
    )
    return scaler.inverse_transform(counterfactual_scaled)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    LOGGER.info("GrCF explainer loaded on device: %s", DEVICE)
