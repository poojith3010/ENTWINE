"""Integration tests for the digital twin data connector."""

import pandas as pd
import pytest
from sqlalchemy import create_engine

from models.twin_data_connector import get_building_state_df, get_database_engine

def test_successful_retrieval_and_structure():
    """Verify data is retrieved, pivoted, sorted, timezone-aware, and mapped."""
    df = get_building_state_df("PH-A-MAIN")
    
    assert not df.empty, "DataFrame should not be empty for PH-A-MAIN."
    
    # 1. Check Index type and Timezone Awareness (UTC)
    assert isinstance(df.index, pd.DatetimeIndex), "Index must be datetime."
    assert df.index.tz is not None, "Timestamps must be timezone-aware."
    assert str(df.index.tz) == 'UTC', "Timestamps must be strictly UTC."
    
    # 2. Check Chronological Sorting
    assert df.index.is_monotonic_increasing, "DataFrame must be sorted chronologically."
    
    # 3. Check for expected pivoted and mapped columns
    assert 'real_power_kw' in df.columns, "Sensor columns did not map to AI snake_case format correctly."

def test_unknown_meter_returns_empty():
    """Verify an unknown meter gracefully returns an empty DataFrame."""
    df = get_building_state_df("UNKNOWN-METER-999")
    assert df.empty, "Unknown meter should return an empty DataFrame."

def test_graceful_database_connection_errors():
    """Verify bad database connections raise a custom ConnectionError."""
    bad_engine = create_engine("postgresql+psycopg2://fake:fake@localhost:5432/fakedb")
    
    with pytest.raises(ConnectionError, match="Failed to retrieve data"):
        get_building_state_df("PH-A-MAIN", engine=bad_engine)