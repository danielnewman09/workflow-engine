"""Record layer mapping query logic.

Encapsulates queries over record_layer_mapping and record_layer_fields:
per-record four-layer field listings with drift analysis, and a
cross-record drift check.  Owned by TraceabilityServer and delegates
SQL construction to record_queries.py.
"""

import sqlite3

from workflow_engine.traceability.table_readers.record_queries import (
    get_record_layer_fields,
    get_record_mapping,
    list_all_record_names,
)
from workflow_engine.utils.sqlite import rows_to_dicts

LAYERS = ("cpp", "sql", "pybind", "pydantic")


class RecordQuerier:
    """Queries over the record_layer_mapping and record_layer_fields tables."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def mappings(self, record_name: str) -> dict:
        """Return all four layers' field lists for a record with drift analysis.

        Compares cpp fields against pybind and pydantic layers, accounting
        for ``_id`` foreign-key naming conventions.

        Returns an ``{"error": ...}`` dict if the record is not found.
        """
        sql, params = get_record_mapping(record_name)
        mapping_row = self.conn.execute(sql, params).fetchone()
        if not mapping_row:
            return {"error": f"Record '{record_name}' not found"}

        layers = {}
        for layer in LAYERS:
            sql, params = get_record_layer_fields(record_name, layer)
            layers[layer] = rows_to_dicts(self.conn.execute(sql, params).fetchall())

        pybind_fields = {f["field_name"] for f in layers["pybind"]}
        pydantic_fields = {f["field_name"] for f in layers["pydantic"]}

        missing_in_pybind = []
        missing_in_pydantic = []
        for f in layers["cpp"]:
            field_name = f["field_name"]
            fk_name = f"{field_name}_id"
            if field_name not in pybind_fields and fk_name not in pybind_fields:
                missing_in_pybind.append(field_name)
            if field_name not in pydantic_fields and fk_name not in pydantic_fields:
                missing_in_pydantic.append(field_name)

        return {
            "record": record_name,
            "pydantic_model": dict(mapping_row).get("pydantic_model"),
            "layers": layers,
            "drift": {
                "missing_in_pybind": missing_in_pybind,
                "missing_in_pydantic": missing_in_pydantic,
                "naming_mismatches": [],
            },
        }

    def check_drift(self) -> list[dict]:
        """Return all records with fields missing from downstream layers."""
        sql, params = list_all_record_names()
        all_records = [row["record_name"] for row in self.conn.execute(sql, params).fetchall()]

        drift_records = []
        for record_name in all_records:
            result = self.mappings(record_name)
            if "error" in result:
                continue

            drift = result["drift"]
            if drift["missing_in_pybind"] or drift["missing_in_pydantic"]:
                drift_records.append(
                    {
                        "record": record_name,
                        "missing_in_pybind": drift["missing_in_pybind"],
                        "missing_in_pydantic": drift["missing_in_pydantic"],
                    }
                )

        return drift_records
