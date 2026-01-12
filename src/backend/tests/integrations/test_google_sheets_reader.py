from __future__ import annotations

from src.backend.v4.integrations.google_sheets_reader import (
    find_value_in_table,
    find_values_for_rows_containing,
)


def test_find_value_in_table_happy_path() -> None:
    rows = [
        ["Account", "Aug. 2025", "Sep. 2025", "Nov. 2025"],
        ["Petty Cash", "1", "2", "3"],
    ]

    res = find_value_in_table(rows=rows, row_key="Petty Cash", col_header="Nov. 2025")
    assert res.value == "3"
    assert res.row_index == 1
    assert res.col_index == 3
    assert res.a1_cell == "D2"


def test_find_value_in_table_fuzzy_matching() -> None:
    rows = [
        ["", "", ""],
        ["Name", "Nov. 2025 Total"],
        ["Petty-cash (CAD)", "10.00"],
    ]

    res = find_value_in_table(rows=rows, row_key="petty cash", col_header="Nov. 2025")
    assert res.value == "10.00"
    assert res.header_row_index == 1
    assert res.matched_col_header == "Nov. 2025 Total"


def test_find_value_in_table_missing_row_or_col() -> None:
    rows = [["Name", "Nov. 2025"], ["Other", "1"]]

    res_row_missing = find_value_in_table(rows=rows, row_key="Petty Cash", col_header="Nov. 2025")
    assert res_row_missing.value is None

    res_col_missing = find_value_in_table(rows=rows, row_key="Other", col_header="Dec. 2025")
    assert res_col_missing.value is None


def test_find_value_in_table_with_explicit_header_row_index() -> None:
    rows = [
        ["", "", ""],
        ["Preparer Name", "Shikha"],
        ["Account", "Oct. 2025", "Nov. 2025"],
        ["Petty Cash", "1", "2"],
    ]

    res = find_value_in_table(
        rows=rows,
        row_key="Petty Cash",
        col_header="Nov. 2025",
        header_row_index=2,
    )
    assert res.value == "2"
    assert res.header_row_index == 2
    assert res.a1_cell == "C4"


def test_find_values_for_rows_containing_returns_all_matches() -> None:
    rows = [
        ["Account", "Nov. 2025"],
        ["AR Clearing Account", "0.00"],
        ["AP clearing account", "1.23"],
        ["Other", "9.99"],
    ]

    matches = find_values_for_rows_containing(
        rows=rows,
        row_substring="clearing account",
        col_header="Nov. 2025",
        header_row_index=0,
    )

    assert [m.value for m in matches] == ["0.00", "1.23"]
    assert [m.a1_cell for m in matches] == ["B2", "B3"]
