-- ENTWINE simulated live-meter storage
-- Keeps synthetic demonstrations separate from real state_telemetry.

CREATE TABLE IF NOT EXISTS simulated_telemetry (
    time TIMESTAMPTZ NOT NULL,
    meter_id BIGINT NOT NULL REFERENCES meters(meter_id) ON DELETE CASCADE,
    simulation_id VARCHAR(100) NOT NULL,
    real_power_kw NUMERIC(14, 6) NOT NULL,
    current_avg_a NUMERIC(14, 6) NOT NULL,
    voltage_ln_avg_v NUMERIC(14, 6) NOT NULL,
    voltage_ll_avg_v NUMERIC(14, 6) NOT NULL,
    frequency_hz NUMERIC(14, 6) NOT NULL,
    power_factor_pct NUMERIC(14, 6) NOT NULL,
    apparent_power_kva NUMERIC(14, 6) NOT NULL,
    known_anomaly BOOLEAN NOT NULL,
    simulation_status VARCHAR(32) NOT NULL DEFAULT 'simulated',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (time, meter_id, simulation_id)
);

CREATE INDEX IF NOT EXISTS idx_simulated_telemetry_simulation
    ON simulated_telemetry (simulation_id, time);
