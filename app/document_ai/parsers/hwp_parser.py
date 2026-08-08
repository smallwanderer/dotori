from hwpx import HwpxDocument
import subprocess
import re
from pathlib import Path

"""
HWPX 변환
"""
class HwpxConversionError(Exception):
    pass

def convert_hwpx_to_markdown(file_path: str) -> str:
    doc = HwpxDocument.open(file_path)
    md = doc.export_markdown()
    return md


"""
HWP 변환
"""
class HwpConversionError(Exception):
    pass


def convert_hwp_to_txt(file_path: str) -> str:
    """
    .hwp 파일을 pyhwp의 hwp5txt CLI를 사용해 텍스트로 변환하여 반환한다.

    Args:
        file_path: 변환할 .hwp 파일 경로

    Returns:
        추출된 전체 텍스트(str)

    Raises:
        FileNotFoundError: 파일이 없을 때
        ValueError: 확장자가 .hwp가 아닐 때
        HwpConversionError: 변환 실패 시
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"파일이 존재하지 않습니다: {file_path}")

    if path.suffix.lower() != ".hwp":
        raise ValueError(f".hwp 파일만 변환할 수 있습니다: {file_path}")

    try:
        result = subprocess.run(
            ["hwp5txt", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except FileNotFoundError as e:
        raise HwpConversionError(
            "hwp5txt 명령어를 찾을 수 없습니다. pyhwp가 설치되어 있는지 확인하세요."
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise HwpConversionError(
            f"hwp -> txt 변환에 실패했습니다. {stderr}"
        ) from e

    text = result.stdout or ""
    text = _normalize_extracted_text(text)

    if not text.strip():
        raise HwpConversionError("텍스트는 추출되었지만 내용이 비어 있습니다.")

    return text


def _normalize_extracted_text(text: str) -> str:
    """
    추출된 텍스트를 검색/청킹에 적합하도록 가볍게 정리한다.
    """
    lines = [line.rstrip() for line in text.splitlines()]

    normalized_lines = []
    blank_streak = 0

    for line in lines:
        stripped = line.strip()

        if not stripped:
            blank_streak += 1
            if blank_streak <= 1:
                normalized_lines.append("")
            continue

        # hwp5txt emits this standalone image sentinel. Captions containing
        # meaningful text are intentionally preserved.
        if re.fullmatch(r"<그림>", stripped):
            continue

        blank_streak = 0
        normalized_lines.append(stripped)

    return "\n".join(normalized_lines).strip()
