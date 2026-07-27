"""Tests for the committed fair-value file.

This file is the one thing a non-technical person edits, straight on
github.com, so it has to accept the loose spellings a human will actually
type and refuse the ones that would silently corrupt a valuation gate.
"""

from __future__ import annotations

import pytest

from screener.fairvalues import (
    FairValue,
    FairValueError,
    load_fair_values,
    save_fair_value,
    write_fair_values,
)


def write(tmp_path, text):
    path = tmp_path / "fair_values.yaml"
    path.write_text(text)
    return path


# ------------------------------------------------------------- reading


def test_missing_file_is_not_an_error(tmp_path):
    """No file just means nobody has checked a fair value yet."""
    assert load_fair_values(tmp_path / "nope.yaml") == {}


def test_empty_file_is_not_an_error(tmp_path):
    assert load_fair_values(write(tmp_path, "")) == {}


def test_reads_the_shorthand_form(tmp_path):
    values = load_fair_values(write(tmp_path, "IBM: 225.00\nNVDA: 250\n"))
    assert values["IBM"].fair_value == 225.0
    assert values["NVDA"].fair_value == 250.0


def test_reads_the_long_form_with_metadata(tmp_path):
    values = load_fair_values(
        write(tmp_path, "TSLA:\n  fair_value: 280.0\n  checked: 2026-07-27\n  note: post-earnings\n")
    )
    entry = values["TSLA"]
    assert entry.fair_value == 280.0
    assert entry.checked == "2026-07-27"
    assert entry.note == "post-earnings"


def test_both_forms_can_coexist(tmp_path):
    values = load_fair_values(write(tmp_path, "IBM: 225\nTSLA:\n  fair_value: 280\n"))
    assert set(values) == {"IBM", "TSLA"}


def test_symbols_are_normalised_to_upper_case(tmp_path):
    assert "IBM" in load_fair_values(write(tmp_path, "ibm: 225\n"))


def test_accepts_a_number_typed_with_currency_formatting(tmp_path):
    """Someone copying off the Morningstar page may well paste '$1,225.50'."""
    values = load_fair_values(write(tmp_path, 'LLY: "$1,225.50"\n'))
    assert values["LLY"].fair_value == 1225.50


# ------------------------------------------------------------- rejecting


def test_rejects_invalid_yaml(tmp_path):
    with pytest.raises(FairValueError, match="not valid YAML"):
        load_fair_values(write(tmp_path, "IBM: [unclosed\n"))


def test_rejects_a_non_mapping_document(tmp_path):
    with pytest.raises(FairValueError, match="map tickers"):
        load_fair_values(write(tmp_path, "- IBM\n- NVDA\n"))


def test_rejects_a_long_form_entry_with_no_fair_value(tmp_path):
    with pytest.raises(FairValueError, match="no `fair_value`"):
        load_fair_values(write(tmp_path, "IBM:\n  checked: 2026-07-27\n"))


def test_rejects_text_where_a_number_belongs(tmp_path):
    with pytest.raises(FairValueError, match="not a number"):
        load_fair_values(write(tmp_path, "IBM: about two hundred\n"))


@pytest.mark.parametrize("bad", ["0", "-15"])
def test_rejects_non_positive_values(tmp_path, bad):
    """A zero or negative fair value would quietly invert the gate."""
    with pytest.raises(FairValueError, match="must be positive"):
        load_fair_values(write(tmp_path, f"IBM: {bad}\n"))


# ------------------------------------------------------------- writing


def test_save_round_trips(tmp_path):
    path = tmp_path / "fair_values.yaml"
    save_fair_value(path, "IBM", 225.0, checked="2026-07-27", note="Q2")
    entry = load_fair_values(path)["IBM"]
    assert (entry.fair_value, entry.checked, entry.note) == (225.0, "2026-07-27", "Q2")


def test_save_keeps_the_other_entries(tmp_path):
    path = tmp_path / "fair_values.yaml"
    save_fair_value(path, "IBM", 225.0)
    save_fair_value(path, "NVDA", 250.0)
    assert set(load_fair_values(path)) == {"IBM", "NVDA"}


def test_save_updates_an_existing_entry_rather_than_duplicating(tmp_path):
    path = tmp_path / "fair_values.yaml"
    save_fair_value(path, "IBM", 225.0)
    save_fair_value(path, "IBM", 230.0)
    values = load_fair_values(path)
    assert len(values) == 1
    assert values["IBM"].fair_value == 230.0


def test_save_defaults_checked_to_today(tmp_path):
    import datetime as dt

    path = tmp_path / "fair_values.yaml"
    save_fair_value(path, "IBM", 225.0)
    assert load_fair_values(path)["IBM"].checked == dt.date.today().isoformat()


def test_written_file_carries_the_editing_instructions(tmp_path):
    """The header is how a non-technical reader knows they may edit it."""
    path = tmp_path / "fair_values.yaml"
    save_fair_value(path, "IBM", 225.0)
    text = path.read_text()
    assert "Edit this file directly on GitHub" in text
    assert text.lstrip().startswith("#")


def test_entries_are_written_in_a_stable_order(tmp_path):
    """Sorted output keeps git diffs to the line that actually changed."""
    path = tmp_path / "fair_values.yaml"
    write_fair_values(
        path,
        {
            "NVDA": FairValue("NVDA", 250.0),
            "AAPL": FairValue("AAPL", 200.0),
            "IBM": FairValue("IBM", 225.0),
        },
    )
    body = [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]
    assert [ln for ln in body if ln.endswith(":")] == ["AAPL:", "IBM:", "NVDA:"]
