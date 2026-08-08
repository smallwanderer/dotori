from __future__ import annotations

import json


def render_table(headers, widths, rows, stdout, row_styles=None) -> None:
    """Compact single-line-per-row table: one header line, one separator
    line, then one line per row. `row_styles`, if given, is a list (parallel
    to `rows`) of per-column style callables (or None per cell); cells are
    padded first and styled after, so ANSI color codes never throw off
    column alignment."""
    line = " ".join(header.ljust(width) for header, width in zip(headers, widths))
    stdout.write(line.rstrip())
    stdout.write("-" * len(line.rstrip()))
    for row_index, row in enumerate(rows):
        styles = row_styles[row_index] if row_styles else None
        cells = []
        for col_index, (value, width) in enumerate(zip(row, widths)):
            padded = str(value)[:width].ljust(width)
            style = styles[col_index] if styles else None
            cells.append(style(padded) if style else padded)
        stdout.write(" ".join(cells).rstrip())


def print_model_table(rows, stdout) -> None:
    headers = ["#", "Model", "Quant", "Size", "Device", "Logical", "Pool Req", "RAM Req", "Backend", "Speed", "Safety", "Fit"]
    widths = [3, 24, 9, 6, 7, 9, 9, 7, 10, 8, 8, 7]
    table_rows = [
        [
            str(row["index"]),
            row["model"] or "-",
            row["quant"] or "-",
            row["size"] or "-",
            row["device"] or "-",
            row["min_mem"] or "-",
            row["rec_mem"] or "-",
            row["ram"] or "-",
            row["backend"] or "-",
            row["speed"] or "-",
            row["safety"] or "-",
            row["fit_status"] or "-",
        ]
        for row in rows
    ]
    render_table(headers, widths, table_rows, stdout)


def model_detail_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "model": row["model"],
        "display_name": row["display_name"],
        "quant": row["quant"],
        "size": row["size"],
        "device": row["device"],
        "min_mem": row["min_mem"],
        "rec_mem": row["rec_mem"],
        "ram": row["ram"],
        "backend": row["backend"],
        "speed": row["speed"],
        "runtime": row["runtime"],
        "base_url": row["base_url"],
        "priority": row["priority"],
        "safety": row["safety"],
        "fit_status": row["fit_status"],
        "recommended": row["recommended"],
        "reason": row["reason"],
        "size_label": row["size_label"],
        "context_length": row["context_length"],
        "concurrency": row["concurrency"],
        "logical_total_memory_mb": row["logical_total_memory_mb"],
        "required_ram_mb": row["required_ram_mb"],
        "required_vram_per_gpu_mb": row["required_vram_per_gpu_mb"],
        "serving_profile": row.get("serving_profile"),
        "description": row["description"],
        "notes": row["notes"],
    }


def json_output(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

