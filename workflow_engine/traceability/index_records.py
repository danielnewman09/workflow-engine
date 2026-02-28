"""
Record Layer Mapping Indexer (Composite Pydantic Models)

Indexes hand-written composite Pydantic models (BodyState, FrameData, etc.)
into the traceability database. These models aggregate generated leaf models
and cannot be auto-generated.

For the four generator-managed layers (cpp, sql, pybind, leaf-pydantic),
use: python scripts/generate_record_layers.py --update-traceability <db_path>
"""

import ast
import re
import sqlite3
from pathlib import Path
from typing import Any

from workflow_engine.traceability.schema import rebuild_fts


def parse_pydantic_models(models_content: str) -> dict[str, dict[str, Any]]:
    """Extract field definitions from hand-written Pydantic BaseModel classes.

    Only returns models that are NOT auto-generated (no "Maps-to:" in docstring).

    Args:
        models_content: Full text of models.py.

    Returns:
        Dict mapping Pydantic class name to:
        - fields: List of dicts with field_name and field_type
        - cpp_record: Inferred C++ record name (or None)
    """
    tree = ast.parse(models_content)
    models = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        is_base_model = any(
            isinstance(base, ast.Name) and base.id == "BaseModel"
            for base in node.bases
        )

        if not is_base_model:
            continue

        docstring = ast.get_docstring(node)
        if docstring and "Maps-to:" in docstring:
            continue

        class_name = node.name
        fields = []

        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                field_name = item.target.id
                field_type = ast.unparse(item.annotation) if item.annotation else "Any"
                fields.append({"field_name": field_name, "field_type": field_type})

        cpp_record = None
        if docstring:
            doc_match = re.search(r"From\s+(\w+Record)", docstring)
            if doc_match:
                cpp_record = doc_match.group(1)

        models[class_name] = {"fields": fields, "cpp_record": cpp_record}

    return models


def populate_composite_models(
    conn: sqlite3.Connection,
    pydantic_models: dict[str, dict[str, Any]],
) -> None:
    """Populate composite Pydantic model fields into record_layer_fields.

    Only touches composite models. Does NOT clear generator-managed layers.

    Args:
        conn: SQLite connection.
        pydantic_models: Dict[class_name, {fields, cpp_record}] from Pydantic.
    """
    conn.execute(
        "DELETE FROM record_layer_fields WHERE layer = 'pydantic' AND notes = 'composite'"
    )

    for class_name, model_info in pydantic_models.items():
        fields = model_info["fields"]
        cpp_record = model_info["cpp_record"]

        for field_info in fields:
            field_name = field_info["field_name"]
            field_type = field_info["field_type"]

            conn.execute(
                """
                INSERT INTO record_layer_fields
                (record_name, layer, field_name, field_type, source_field, notes)
                VALUES (?, 'pydantic', ?, ?, ?, 'composite')
                """,
                (
                    cpp_record or class_name,
                    field_name,
                    field_type,
                    None,
                ),
            )

    conn.commit()


def index_records(
    conn: sqlite3.Connection,
    repo_root: str,
    models_path: str = "replay/replay/models.py",
    generated_models_path: str = "replay/replay/generated_models.py",
) -> dict:
    """Index composite Pydantic models into the traceability database.

    Args:
        conn: Open SQLite connection to the traceability database.
        repo_root: Path to the repository root.
        models_path: Relative path to the hand-written Pydantic models file.
        generated_models_path: Relative path to the generated models file.

    Returns:
        Dict with 'composite_count' and 'generated_count' keys.
    """
    root = Path(repo_root)

    pydantic_models = {}
    abs_models_path = root / models_path
    if abs_models_path.exists():
        models_content = abs_models_path.read_text()
        pydantic_models = parse_pydantic_models(models_content)

    generated_count = 0
    abs_gen_path = root / generated_models_path
    if abs_gen_path.exists():
        gen_content = abs_gen_path.read_text()
        gen_tree = ast.parse(gen_content)
        for node in ast.walk(gen_tree):
            if isinstance(node, ast.ClassDef):
                generated_count += 1

    populate_composite_models(conn, pydantic_models)
    rebuild_fts(conn)

    return {"composite_count": len(pydantic_models), "generated_count": generated_count}
