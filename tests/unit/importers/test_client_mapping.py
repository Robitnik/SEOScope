from __future__ import annotations

import json
from pathlib import Path

from app.importers.client_mapping import load_client_map, normalize_code, normalize_domain


def _write_clients_json(tmp_path: Path, data: dict) -> None:
    (tmp_path / "clients.json").write_text(json.dumps(data))


def test_normalize_domain_variants():
    assert normalize_domain("sc-domain:example.com") == "example.com"
    assert normalize_domain("https://example.com/") == "example.com"
    assert normalize_domain("https://www.example.com/") == "example.com"
    assert normalize_domain("example.com") == "example.com"
    assert normalize_domain("") is None
    assert normalize_domain(None) is None  # type: ignore[arg-type]


def test_normalize_code_collapses_space_and_dash():
    assert normalize_code("SAPPHIRE NEW YORK") == normalize_code("sapphire-new-york")
    assert normalize_code("PJC") == "PJC"


def test_maps_gsc_and_ga4_to_domain(tmp_path):
    _write_clients_json(
        tmp_path,
        {
            "ACME": {
                "gsc": {"connection_key": "x@y.com", "account_id": "sc-domain:acme.com"},
                "ga4": {"connection_key": "x@y.com", "account_id": "properties/123"},
                "ninja_report_name": "ACME report",
            }
        },
    )
    client_map = load_client_map(tmp_path)
    assert client_map.entries["ACME"].domain == "acme.com"
    assert client_map.entries["ACME"].ga4_account_id == "properties/123"
    assert client_map.domain_to_codes == {"acme.com": ["ACME"]}
    assert client_map.conflicts == {}
    assert client_map.unmappable_codes == []


def test_grp_style_url_prefix_property_maps_same_as_sc_domain(tmp_path):
    _write_clients_json(tmp_path, {"GRP": {"gsc": {"account_id": "https://grillpointnyc.com/"}}})
    client_map = load_client_map(tmp_path)
    assert client_map.entries["GRP"].domain == "grillpointnyc.com"


def test_ga4_only_entry_is_unmappable(tmp_path):
    _write_clients_json(tmp_path, {"GA4ONLY": {"ga4": {"account_id": "properties/999"}}})
    client_map = load_client_map(tmp_path)
    assert client_map.unmappable_codes == ["GA4ONLY"]
    assert client_map.entries["GA4ONLY"].domain is None


def test_entry_with_no_provider_blocks_is_not_unmappable_or_mapped(tmp_path):
    _write_clients_json(tmp_path, {"EMPTY": {}})
    client_map = load_client_map(tmp_path)
    assert client_map.entries["EMPTY"].domain is None
    assert client_map.unmappable_codes == []


def test_conflicting_codes_for_same_domain(tmp_path):
    _write_clients_json(
        tmp_path,
        {
            "A": {"gsc": {"account_id": "sc-domain:acme.com"}},
            "B": {"gsc": {"account_id": "https://acme.com/"}},
        },
    )
    client_map = load_client_map(tmp_path)
    assert client_map.conflicts == {"acme.com": ["A", "B"]}


def test_missing_clients_json_is_empty_map(tmp_path):
    client_map = load_client_map(tmp_path)
    assert client_map.entries == {}
    assert client_map.conflicts == {}
    assert client_map.unmappable_codes == []
