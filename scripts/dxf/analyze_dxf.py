"""
AutoCAD DXF 圖紙自動化統計工具
用法: python analyze_dxf.py <path_to.dxf> [--keywords GI PE] [--pipe-layers TG-PIPE] [--fitting-layers TG-RBOX16QR]

兩種統計模式：
  管道圖層 (--pipe-layers)    → 統計 LINE / LWPOLYLINE 線段長度（米）
  管件圖層 (--fitting-layers) → 統計 INSERT（圖塊）數量（個）
"""

import sys
import math
import argparse
import re
from collections import defaultdict

try:
    import ezdxf
    import ezdxf.recover
except ImportError:
    # Autoscale may run this child process without the workspace Python
    # user-site packages. The ASCII DXF fallback below keeps core analysis
    # available in that environment instead of failing at import time.
    ezdxf = None

DEFAULT_PIPE_LAYERS = [
    "TG-PIPE DN250", "TG-PIPE DN200", "TG-PIPE DN150", "TG-PIPE DN100",
    "TG-PIPE DN80",  "TG-PIPE DN50",  "TG-PIPE DN32",  "TG-PIPE DN25",
    "TG-PIPE DN20",  "TG-PIPE DN15",
]

DEFAULT_FITTING_LAYERS = [
    "TG-RBOX16QR", "TG-RBOX16QL",
    "TG-TRJW222TFQL", "TG-TRJW162TFQL",
    "TG-U8", "TG-U16", "TG-U25",
    "TG-DN250 GV", "TG-DN200 GV", "TG-DN150 GV",
    "TG-DN100 GV", "TG-DN80 GV",  "TG-DN50 GV",
    "TG-EV",
]

DEFAULT_KEYWORDS = ["GI", "PE", "喉", "鍍鋅", "銅", "不鏽鋼", "閘", "曲", "叉", "梳杰"]

UNIT_TO_MM = 1.0


def entity_length(entity) -> float:
    """Return the length of a LINE or LWPOLYLINE entity in drawing units."""
    try:
        dxftype = entity.dxftype()
        if dxftype == "LINE":
            s = entity.dxf.start
            e = entity.dxf.end
            return math.sqrt((e.x - s.x)**2 + (e.y - s.y)**2 + (e.z - s.z)**2)
        if dxftype == "LWPOLYLINE":
            pts = list(entity.get_points("xy"))
            total = 0.0
            for i in range(len(pts) - 1):
                dx = pts[i+1][0] - pts[i][0]
                dy = pts[i+1][1] - pts[i][1]
                total += math.sqrt(dx*dx + dy*dy)
            if entity.closed and len(pts) > 1:
                dx = pts[0][0] - pts[-1][0]
                dy = pts[0][1] - pts[-1][1]
                total += math.sqrt(dx*dx + dy*dy)
            return total
    except Exception:
        pass
    return 0.0


def raw_text_keywords(dxf_path: str, keywords: list) -> dict:
    """
    Fallback: scan raw DXF text for keywords.
    Looks for text content in DXF group code 1 (TEXT value) lines.
    """
    counts: dict = defaultdict(int)
    try:
        with open(dxf_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        for kw in keywords:
            counts[kw] = len(re.findall(re.escape(kw), content, re.IGNORECASE))
    except Exception:
        pass
    return counts


def raw_entity_analysis(dxf_path: str, keywords: list, pipe_layers: list, fitting_layers: list):
    """
    Dependency-free fallback for ASCII DXF files.

    DXF stores entities as code/value pairs. This parser intentionally covers
    the entity fields needed by this tool: TEXT/MTEXT, LINE, LWPOLYLINE and
    INSERT. Binary DXF files still require ezdxf.
    """
    keyword_counts: dict = defaultdict(int)
    text_samples: dict = defaultdict(list)
    pipe_lengths: dict = defaultdict(float)
    pipe_counts: dict = defaultdict(int)
    fitting_counts: dict = defaultdict(int)
    all_line_layers: dict = defaultdict(int)

    pipe_upper = [layer.upper() for layer in pipe_layers]
    fitting_upper = [layer.upper() for layer in fitting_layers]

    def matches_layer(layer_name: str, candidates: list) -> bool:
        upper = layer_name.upper()
        return upper in candidates or any(upper.startswith(candidate) for candidate in candidates)

    def number(values: list, index: int, default: float = 0.0) -> float:
        try:
            return float(values[index])
        except (IndexError, TypeError, ValueError):
            return default

    def entity_length(entity_type: str, values: dict) -> float:
        if entity_type == "LINE":
            x1, y1, z1 = number(values.get("10", []), 0), number(values.get("20", []), 0), number(values.get("30", []), 0)
            x2, y2, z2 = number(values.get("11", []), 0), number(values.get("21", []), 0), number(values.get("31", []), 0)
            return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2)
        if entity_type == "LWPOLYLINE":
            xs, ys = values.get("10", []), values.get("20", [])
            points = [(number(xs, i), number(ys, i)) for i in range(min(len(xs), len(ys)))]
            total = sum(
                math.sqrt((points[i + 1][0] - points[i][0]) ** 2 + (points[i + 1][1] - points[i][1]) ** 2)
                for i in range(len(points) - 1)
            )
            flags = int(number(values.get("70", []), 0))
            if flags & 1 and len(points) > 1:
                total += math.sqrt(
                    (points[0][0] - points[-1][0]) ** 2 + (points[0][1] - points[-1][1]) ** 2
                )
            return total
        return 0.0

    try:
        with open(dxf_path, "r", encoding="utf-8", errors="replace") as file:
            raw_lines = [line.rstrip("\r\n") for line in file]
    except Exception:
        return keyword_counts, text_samples, pipe_lengths, pipe_counts, fitting_counts, all_line_layers

    in_entities = False
    awaiting_section_name = False
    current_type = None
    current_values = defaultdict(list)

    def flush_entity():
        if not current_type:
            return
        layer_name = (current_values.get("8", [""])[0] or "").strip()
        if current_type in ("TEXT", "MTEXT"):
            text = "".join(current_values.get("1", []) + current_values.get("3", []))
            for keyword in keywords:
                if keyword.upper() in text.upper():
                    keyword_counts[keyword] += 1
                    if len(text_samples[keyword]) < 3:
                        text_samples[keyword].append(text[:60].replace("\n", " "))
        if current_type in ("LINE", "LWPOLYLINE"):
            all_line_layers[layer_name] += 1
            if matches_layer(layer_name, pipe_upper):
                pipe_lengths[layer_name] += entity_length(current_type, current_values)
                pipe_counts[layer_name] += 1
        if current_type == "INSERT" and matches_layer(layer_name, fitting_upper):
            fitting_counts[layer_name] += 1

    pair_count = len(raw_lines) - (len(raw_lines) % 2)
    for index in range(0, pair_count, 2):
        code = raw_lines[index].strip()
        value = raw_lines[index + 1]
        if code == "0":
            if value == "SECTION":
                if in_entities:
                    flush_entity()
                in_entities = False
                awaiting_section_name = True
                current_type = None
                current_values = defaultdict(list)
            elif value == "ENDSEC":
                if in_entities:
                    flush_entity()
                in_entities = False
                awaiting_section_name = False
                current_type = None
                current_values = defaultdict(list)
            elif in_entities:
                flush_entity()
                current_type = value.strip().upper()
                current_values = defaultdict(list)
            continue
        if awaiting_section_name and code == "2":
            in_entities = value.strip().upper() == "ENTITIES"
            awaiting_section_name = False
            continue
        if in_entities and current_type:
            current_values[code].append(value)

    if in_entities:
        flush_entity()

    return keyword_counts, text_samples, pipe_lengths, pipe_counts, fitting_counts, all_line_layers


def safe_iter_msp(msp):
    """Iterate modelspace entities one by one, skipping any that raise exceptions."""
    try:
        it = iter(msp)
    except Exception:
        return
    while True:
        try:
            entity = next(it)
            yield entity
        except StopIteration:
            break
        except Exception:
            continue


def analyze(dxf_path: str, keywords: list, pipe_layers: list, fitting_layers: list):
    print(f"\n讀取檔案：{dxf_path}")

    ezdxf_ok = False
    doc = None
    try:
        if ezdxf is None:
            raise RuntimeError("ezdxf 未安裝，使用內置 ASCII DXF 分析器")
        doc, auditor = ezdxf.recover.readfile(dxf_path)
        if auditor.has_errors:
            print(f"[警告] DXF 有 {len(auditor.errors)} 個修復問題，繼續分析...", file=sys.stderr)
        ezdxf_ok = True
    except Exception as e:
        print(f"[警告] {e}，切換為內置原始格式模式", file=sys.stderr)

    pipe_upper    = [l.upper() for l in pipe_layers]
    fitting_upper = [l.upper() for l in fitting_layers]

    # ── 1. 文字統計 ───────────────────────────────────────────────────────────
    keyword_counts: dict = defaultdict(int)
    text_samples:   dict = defaultdict(list)
    total_text_entities = 0
    used_raw_fallback = False
    pipe_lengths: dict = defaultdict(float)
    pipe_counts:  dict = defaultdict(int)
    all_line_layers: dict = defaultdict(int)   # discovery: all layers with LINE/LWPOLYLINE
    fitting_counts: dict = defaultdict(int)

    if ezdxf_ok and doc is not None:
        msp = doc.modelspace()
        for entity in safe_iter_msp(msp):
            try:
                raw = ""
                if entity.dxftype() == "TEXT":
                    raw = entity.dxf.text or ""
                elif entity.dxftype() == "MTEXT":
                    raw = entity.plain_mtext() if hasattr(entity, "plain_mtext") else (entity.text or "")
                if not raw:
                    continue
                total_text_entities += 1
                for kw in keywords:
                    if kw.upper() in raw.upper():
                        keyword_counts[kw] += 1
                        if len(text_samples[kw]) < 3:
                            text_samples[kw].append(raw[:60].replace("\n", " "))
            except Exception:
                continue

    # If ezdxf failed OR found no text at all, try raw text scan
    if not ezdxf_ok:
        (
            keyword_counts,
            text_samples,
            raw_pipe_lengths,
            raw_pipe_counts,
            fitting_counts,
            raw_line_layers,
        ) = raw_entity_analysis(dxf_path, keywords, pipe_layers, fitting_layers)
        pipe_lengths.update(raw_pipe_lengths)
        pipe_counts.update(raw_pipe_counts)
        all_line_layers.update(raw_line_layers)
        used_raw_fallback = True
    elif total_text_entities == 0:
        raw_counts = raw_text_keywords(dxf_path, keywords)
        if sum(raw_counts.values()) > 0:
            keyword_counts = raw_counts
            used_raw_fallback = True

    # ── 2. 管道線段長度統計 ───────────────────────────────────────────────────
    if ezdxf_ok and doc is not None:
        msp = doc.modelspace()
        for entity in safe_iter_msp(msp):
            try:
                if entity.dxftype() not in ("LINE", "LWPOLYLINE"):
                    continue
                ent_layer = (entity.dxf.layer or "")
                all_line_layers[ent_layer] += 1
                ent_upper = ent_layer.upper()
                if ent_upper in pipe_upper or any(ent_upper.startswith(l) for l in pipe_upper):
                    length = entity_length(entity)
                    pipe_lengths[ent_layer] += length
                    pipe_counts[ent_layer]  += 1
            except Exception:
                continue

    # ── 3. 管件數量統計 ───────────────────────────────────────────────────────
    all_insert_layers: dict = defaultdict(int)  # discovery: all layers with INSERT

    if ezdxf_ok and doc is not None:
        msp = doc.modelspace()
        for entity in safe_iter_msp(msp):
            try:
                ent_layer = (entity.dxf.layer or "")
                ent_upper = ent_layer.upper()
                if entity.dxftype() == "INSERT":
                    all_insert_layers[ent_layer] += 1
                if ent_upper in fitting_upper or any(ent_upper.startswith(l) for l in fitting_upper):
                    if entity.dxftype() in ("INSERT", "POINT", "TEXT", "MTEXT"):
                        fitting_counts[ent_layer] += 1
            except Exception:
                continue

    # ── 4. 輸出結果 ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("【文字統計】")
    if used_raw_fallback:
        print("  （內置 ASCII DXF 分析模式；無需 ezdxf 套件）")
    else:
        print(f"  圖紙中文字實體總數：{total_text_entities}")
    print()
    print(f"  {'關鍵字':<16} {'出現次數':>8}   樣本（最多 3 條）")
    print(f"  {'-'*16} {'-'*8}   {'-'*30}")
    for kw in keywords:
        count   = keyword_counts.get(kw, 0)
        samples = text_samples.get(kw, [])
        sample_str = " | ".join(samples) if samples else "（無）"
        print(f"  {kw:<16} {count:>8}   {sample_str}")

    print()
    print("=" * 60)
    print("【管道線段長度統計（圖紙單位）】")
    if pipe_lengths:
        print(f"\n  {'圖層':<25} {'線段數量':>10} {'總長度':>14}")
        print(f"  {'-'*25} {'-'*10} {'-'*14}")
        for layer_name, total_len in sorted(pipe_lengths.items()):
            count = pipe_counts[layer_name]
            print(f"  {layer_name:<25} {count:>10} {total_len:>14.3f}")
        grand_total = sum(pipe_lengths.values())
        print(f"\n  {'合計':<25} {sum(pipe_counts.values()):>10} {grand_total:>14.3f}")
    else:
        print(f"\n  在管道圖層 {pipe_layers} 中找不到線段實體。")
        # Discovery: show top line layers actually found
        if all_line_layers:
            top = sorted(all_line_layers.items(), key=lambda x: -x[1])[:20]
            print(f"\n  【圖則實際存在的 LINE 圖層（前 20 個）】")
            for lname, cnt in top:
                print(f"  DISCOVERED_LAYER\t{lname}\t{cnt}")

    print()
    print("=" * 60)
    print("【管件數量統計（圖塊 INSERT 數量）】")
    if fitting_counts:
        print(f"\n  {'圖層':<25} {'數量':>10}")
        print(f"  {'-'*25} {'-'*10}")
        for layer_name, count in sorted(fitting_counts.items()):
            print(f"  {layer_name:<25} {count:>10}")
        print(f"\n  {'合計':<25} {sum(fitting_counts.values()):>10}")
    else:
        print(f"\n  在管件圖層 {fitting_layers} 中找不到 INSERT 實體。")
        print("  提示：管件若用 POINT 或其他方式標記，請告知以調整統計方法。")

    print()
    print("=" * 60)
    print("【Google Sheets 貼上格式（Tab 分隔，直接複製）】")
    print()
    print("管道線段長度統計")
    print("圖層\t線段數量\t總長度（圖紙單位）")
    for layer_name, total_len in sorted(pipe_lengths.items()):
        print(f"{layer_name}\t{pipe_counts[layer_name]}\t{total_len:.3f}")

    print()
    print("管件數量統計")
    print("圖層\t數量")
    for layer_name, count in sorted(fitting_counts.items()):
        print(f"{layer_name}\t{count}")

    print()
    print("=" * 60)
    print("完成。")


def main():
    parser = argparse.ArgumentParser(description="AutoCAD DXF 圖紙統計工具（管道長度 + 管件數量）")
    parser.add_argument("dxf", nargs="?", default="test.dxf", help="DXF 檔案路徑")
    parser.add_argument("--keywords",       nargs="+", default=DEFAULT_KEYWORDS,       help="文字關鍵字")
    parser.add_argument("--pipe-layers",    nargs="+", default=DEFAULT_PIPE_LAYERS,    help="管道圖層（數線段長度）")
    parser.add_argument("--fitting-layers", nargs="+", default=DEFAULT_FITTING_LAYERS, help="管件圖層（數 INSERT 數量）")
    parser.add_argument("--layers", nargs="+", default=None, help="[舊版相容] 管道圖層（同 --pipe-layers）")
    args = parser.parse_args()

    pipe_layers = args.layers if args.layers else args.pipe_layers
    analyze(args.dxf, args.keywords, pipe_layers, args.fitting_layers)


if __name__ == "__main__":
    main()
