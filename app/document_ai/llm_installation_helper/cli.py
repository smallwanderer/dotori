from __future__ import annotations

import json


def print_model_table(rows, stdout) -> None:
    headers = ["#", "Model", "Quant", "Size", "Device", "Logical", "Pool Req", "RAM Req", "Backend", "Speed", "Safety", "Fit"]
    widths = [3, 24, 9, 6, 7, 9, 9, 7, 10, 8, 8, 7]
    line = " ".join(header.ljust(width) for header, width in zip(headers, widths))
    stdout.write(line.rstrip())
    stdout.write("-" * len(line.rstrip()))
    for row in rows:
        values = [
            str(row["index"]),
            row["model"],
            row["quant"],
            row["size"],
            row["device"],
            row["min_mem"],
            row["rec_mem"],
            row["ram"],
            row["backend"],
            row["speed"],
            row["safety"],
            row["fit_status"],
        ]
        stdout.write(" ".join(value[:width].ljust(width) for value, width in zip(values, widths)).rstrip())


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
        "serving_profile": row["serving_profile"],
        "description": row["description"],
        "notes": row["notes"],
    }


def json_output(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

