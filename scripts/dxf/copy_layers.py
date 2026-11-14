#!/usr/bin/env python3
"""Copy modelspace layers or Paper Space title blocks between DXF drawings."""

import argparse
import json
import os
import sys
import tempfile

import ezdxf
import ezdxf.recover
from ezdxf.addons import Importer


TEXT_ENTITIES = {"MTEXT", "TEXT", "ATTRIB", "ATTDEF"}


def _group_code(raw_line):
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


def _repair_orphan_text_lines(source_path):
    """Restore missing text group codes emitted by some DWG converters."""
    with open(source_path, "rb") as source:
        lines = source.read().splitlines()

    repaired = []
    current_entity = ""
    has_primary_text = False
    repair_count = 0
    index = 0

    while index < len(lines):
        code_line = lines[index]
        code = _group_code(code_line)
        if code is None:
            if current_entity not in TEXT_ENTITIES:
                raise ValueError(
                    f"DXF 結構損壞：第 {index + 1} 行不是有效 group code"
                )
            # LibreDWG can emit an MTEXT continuation line without its leading
            # group code. Preserve the text as code 1 (first chunk) or 3
            # (continuation) rather than dropping drawing content.
            repaired.extend([
                b"  3" if has_primary_text else b"  1",
                code_line,
            ])
            has_primary_text = True
            repair_count += 1
            index += 1
            continue

        if index + 1 >= len(lines):
            raise ValueError("DXF 結構損壞：最後一個 group code 缺少內容")

        value_line = lines[index + 1]
        repaired.extend([code_line, value_line])
        if code == 0:
            current_entity = value_line.strip().decode("ascii", errors="ignore").upper()
            has_primary_text = False
        elif current_entity in TEXT_ENTITIES and code in (1, 3):
            has_primary_text = True
        index += 2

    if repair_count == 0:
        raise ValueError("DXF 結構損壞，而且找不到可安全修復的文字行")

    handle, repaired_path = tempfile.mkstemp(prefix="dxf-text-repair-", suffix=".dxf")
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(b"\n".join(repaired))
            output.write(b"\n")
    except Exception:
        os.unlink(repaired_path)
        raise
    return repaired_path, repair_count


def read_document(source_path):
    try:
        document, _auditor = ezdxf.recover.readfile(source_path)
        return document
    except Exception as original_error:
        if "invalid group code" not in str(original_error).lower():
            raise

    repaired_path, repair_count = _repair_orphan_text_lines(source_path)
    try:
        document, auditor = ezdxf.recover.readfile(repaired_path)
        if auditor.has_errors:
            print(
                f"[警告] 已修復 {repair_count} 行文字；仍有 "
                f"{len(auditor.errors)} 個 DXF 問題",
                file=sys.stderr,
            )
        else:
            print(f"[警告] 已修復 {repair_count} 行缺少 group code 的文字", file=sys.stderr)
        return document
    finally:
        os.unlink(repaired_path)


def layer_counts(doc):
    counts = {}
    types = {}
    for entity in doc.modelspace():
        layer = entity.dxf.get("layer", "0")
        counts[layer] = counts.get(layer, 0) + 1
        layer_types = types.setdefault(layer, {})
        entity_type = entity.dxftype()
        layer_types[entity_type] = layer_types.get(entity_type, 0) + 1

    result = []
    for layer in doc.layers:
        name = layer.dxf.name
        count = counts.get(name, 0)
        if count <= 0:
            continue
        top_types = sorted(types.get(name, {}).items(), key=lambda item: (-item[1], item[0]))
        result.append({
            "name": name,
            "count": count,
            "entityTypes": [{"type": key, "count": value} for key, value in top_types[:6]],
            "isOff": layer.is_off(),
            "isFrozen": layer.is_frozen(),
            "isLocked": layer.is_locked(),
        })
    return sorted(result, key=lambda item: (-item["count"], item["name"].lower()))


def entity_text_fields(entity, layout_name, entity_index, doc):
    fields = []
    entity_type = entity.dxftype()
    if entity_type in ("TEXT", "MTEXT", "ATTRIB"):
        fields.append({
            "id": entity.dxf.handle,
            "layout": layout_name,
            "entityIndex": entity_index,
            "kind": entity_type,
            "tag": entity.dxf.tag if entity.dxf.hasattr("tag") else "",
            "text": entity.dxf.get("text", ""),
            "layer": entity.dxf.get("layer", "0"),
        })
    if entity_type == "INSERT":
        for attrib_index, attrib in enumerate(entity.attribs):
            if attrib.dxftype() == "ATTRIB":
                fields.append({
                    "id": attrib.dxf.handle,
                    "layout": layout_name,
                    "entityIndex": entity_index,
                    "attributeIndex": attrib_index,
                    "kind": "ATTRIB",
                    "tag": attrib.dxf.get("tag", ""),
                    "text": attrib.dxf.get("text", ""),
                    "block": entity.dxf.get("name", ""),
                    "layer": entity.dxf.get("layer", "0"),
                })
        block_name = entity.dxf.get("name", "")
        if block_name:
            try:
                block_layout = doc.blocks.get(block_name)
                for block_entity in block_layout:
                    if block_entity.dxftype() in ("TEXT", "MTEXT"):
                        fields.append({
                            "id": block_entity.dxf.handle,
                            "layout": layout_name,
                            "entityIndex": entity_index,
                            "kind": block_entity.dxftype(),
                            "tag": "",
                            "text": block_entity.dxf.get("text", ""),
                            "block": block_name,
                            "layer": block_entity.dxf.get("layer", "0"),
                        })
            except KeyError:
                pass
    return fields


def list_paperspace(source_path):
    source = read_document(source_path)
    layouts = []
    for layout in source.layouts:
        if layout.name.lower() == "model":
            continue
        entities = list(layout)
        text_fields = []
        seen_text_ids = set()
        for index, entity in enumerate(entities):
            for field in entity_text_fields(entity, layout.name, index, source):
                if field["id"] not in seen_text_ids:
                    text_fields.append(field)
                    seen_text_ids.add(field["id"])
        layouts.append({
            "name": layout.name,
            "entityCount": len(entities),
            "textCount": len(text_fields),
            "textFields": text_fields[:500],
        })
    return {"dxfVersion": source.dxfversion, "layouts": layouts}


def list_layers(source_path):
    source = read_document(source_path)
    return {
        "dxfVersion": source.dxfversion,
        "layers": layer_counts(source),
    }


def copy_layers(source_path, target_path, output_path, selected_layers, dxf_version=None):
    source = read_document(source_path)
    target = read_document(target_path)
    selected = set(selected_layers)
    entities = [
        entity for entity in source.modelspace()
        if entity.dxf.get("layer", "0") in selected
    ]
    if not entities:
        raise ValueError("所選 layer 在舊圖 Model Space 內沒有可複製物件")

    before = len(target.modelspace())
    importer = Importer(source, target)
    importer.import_entities(entities, target.modelspace())
    importer.finalize()
    after = len(target.modelspace())
    copied = max(0, after - before)
    if copied <= 0:
        raise ValueError("所選 layer 只包含目前不支援複製的 AutoCAD 物件")

    if dxf_version:
        target.dxfversion = dxf_version
    target.saveas(output_path)
    return {
        "selectedLayers": sorted(selected),
        "requestedEntityCount": len(entities),
        "copiedEntityCount": copied,
        "skippedEntityCount": max(0, len(entities) - copied),
    }


def set_entity_text(entity, value):
    if entity.dxftype() in ("TEXT", "MTEXT", "ATTRIB"):
        entity.dxf.text = value


def copy_paperspace(
    source_path,
    target_path,
    output_path,
    source_layout_name,
    target_layout_name,
    text_updates,
    dxf_version=None,
):
    source = read_document(source_path)
    target = read_document(target_path)
    try:
        source_layout = source.layouts.get(source_layout_name)
    except KeyError as exc:
        raise ValueError(f"舊圖找不到 Paper Space Layout：{source_layout_name}") from exc
    try:
        target_layout = target.layouts.get(target_layout_name)
    except KeyError:
        target_layout = target.layouts.new(target_layout_name)

    updates = {}
    for item in text_updates:
        if not isinstance(item, dict):
            continue
        field_id = item.get("id")
        value = item.get("text")
        if isinstance(field_id, str) and isinstance(value, str):
            updates[field_id] = value[:2000]

    updated_text = 0
    for field_id, value in updates.items():
        entity = source.entitydb.get(field_id)
        if entity is not None and entity.dxftype() in ("TEXT", "MTEXT", "ATTRIB"):
            set_entity_text(entity, value)
            updated_text += 1

    importer = Importer(source, target)
    copied = 0
    skipped = 0
    for source_entity in list(source_layout):
        before_handles = {
            entity.dxf.handle
            for entity in target_layout
            if entity.dxf.hasattr("handle")
        }
        importer.import_entity(source_entity, target_layout)
        new_entities = [
            entity
            for entity in target_layout
            if entity.dxf.hasattr("handle") and entity.dxf.handle not in before_handles
        ]
        if not new_entities:
            skipped += 1
            continue
        copied += 1

    importer.finalize()
    if copied <= 0:
        raise ValueError("舊圖所選 Layout 沒有可複製的 Paper Space 物件")
    if dxf_version:
        target.dxfversion = dxf_version
    target.saveas(output_path)
    return {
        "sourceLayout": source_layout_name,
        "targetLayout": target_layout_name,
        "copiedEntityCount": copied,
        "skippedEntityCount": skipped,
        "updatedTextCount": updated_text,
    }


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("source")

    copy_parser = subparsers.add_parser("copy")
    copy_parser.add_argument("source")
    copy_parser.add_argument("target")
    copy_parser.add_argument("output")
    copy_parser.add_argument("--layers-json", required=True)
    copy_parser.add_argument("--dxf-version")

    paperspace_list_parser = subparsers.add_parser("list-paperspace")
    paperspace_list_parser.add_argument("source")

    paperspace_copy_parser = subparsers.add_parser("copy-paperspace")
    paperspace_copy_parser.add_argument("source")
    paperspace_copy_parser.add_argument("target")
    paperspace_copy_parser.add_argument("output")
    paperspace_copy_parser.add_argument("--source-layout", required=True)
    paperspace_copy_parser.add_argument("--target-layout", required=True)
    paperspace_copy_parser.add_argument("--text-updates-json", required=True)
    paperspace_copy_parser.add_argument("--dxf-version")

    args = parser.parse_args()
    if args.command == "list":
        result = list_layers(args.source)
    elif args.command == "list-paperspace":
        result = list_paperspace(args.source)
    elif args.command == "copy":
        selected_layers = json.loads(args.layers_json)
        if not isinstance(selected_layers, list) or not all(isinstance(item, str) for item in selected_layers):
            raise ValueError("layers-json 必須是 layer 名稱陣列")
        result = copy_layers(
            args.source,
            args.target,
            args.output,
            selected_layers,
            args.dxf_version,
        )
    else:
        text_updates = json.loads(args.text_updates_json)
        if not isinstance(text_updates, list):
            raise ValueError("text-updates-json 必須是陣列")
        result = copy_paperspace(
            args.source,
            args.target,
            args.output,
            args.source_layout,
            args.target_layout,
            text_updates,
            args.dxf_version,
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)