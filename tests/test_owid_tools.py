import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
import asyncio

import owid_tools
from owid_tools import _fuzzy_match_country, _detect_cols, Tools


def test_fuzzy_match_country():
    available = ["United States", "United Kingdom", "Italy", "Czechia"]

    # Exact match
    assert _fuzzy_match_country("Italy", available) == "Italy"

    # Case insensitive exact match
    assert _fuzzy_match_country("italy", available) == "Italy"

    # Alias / abbreviation match
    assert _fuzzy_match_country("USA", available) == "United States"
    assert _fuzzy_match_country("UK", available) == "United Kingdom"
    assert _fuzzy_match_country("Czech Republic", available) == "Czechia"

    # Fuzzy match
    assert _fuzzy_match_country("Ital", available) == "Italy"

    # No match
    assert _fuzzy_match_country("Atlantis", available) == "Atlantis"


def test_detect_cols():
    # Test year column
    df1 = pd.DataFrame({"Entity": [], "Code": [], "Year": []})
    c1, y1 = _detect_cols(df1)
    assert c1 == "Entity"
    assert y1 == "Year"

    # Test date column
    df2 = pd.DataFrame({"country": [], "Date": []})
    c2, y2 = _detect_cols(df2)
    assert c2 == "country"
    assert y2 == "Date"

    # Test day column
    df3 = pd.DataFrame({"entities": [], "day": []})
    c3, y3 = _detect_cols(df3)
    assert c3 == "entities"
    assert y3 == "day"


@pytest.mark.asyncio
async def test_caching_and_get_data():
    # Clear cache before test
    owid_tools._cached_fetch_df.cache_clear()

    mock_data = {
        "entities": ["Italy", "France", "Italy", "France"],
        "years": [2000, 2000, 2001, 2001],
        "value": [10, 20, 15, 25],
    }
    mock_df = pd.DataFrame(mock_data)

    with patch("owid_tools.fetch") as mock_fetch:
        mock_fetch.return_value = mock_df

        tools = Tools()
        tools.valves.max_table_rows = 10

        # Call 1
        res1 = await tools.get_owid_data("fake-slug", country="Italy")

        # Call 2
        res2 = await tools.get_owid_data("fake-slug", country="France")

        # fetch should only be called once because the second call hits the cache
        mock_fetch.assert_called_once_with("fake-slug")

        assert "Italy" in res1
        assert "2000" in res1
        assert "10" in res1
        assert "France" not in res1

        assert "France" in res2
        assert "2001" in res2
        assert "25" in res2


@pytest.mark.asyncio
async def test_date_filtering():
    # Test that the string date filtering logic works in get_owid_data
    owid_tools._cached_fetch_df.cache_clear()

    mock_data = {
        "entities": ["World", "World", "World"],
        "date": ["2020-01-01", "2020-02-01", "2020-03-01"],
        "value": [100, 200, 300],
    }
    mock_df = pd.DataFrame(mock_data)

    with patch("owid_tools.fetch") as mock_fetch:
        mock_fetch.return_value = mock_df
        tools = Tools()

        # Filter range: start and end bounds
        res = await tools.get_owid_data(
            "covid", country="World", year_start="2020-01-15", year_end="2020-02-15"
        )

        assert "2020-02-01" in res
        assert "2020-01-01" not in res
        assert "2020-03-01" not in res
