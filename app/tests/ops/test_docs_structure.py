"""Structure guards for dotori-docs.

The rules are written down in dotori-docs/architecture/extending/writing-docs.md;
this file makes the mechanical ones fail a test run instead of relying on
review: every relative link and heading anchor resolves, a page's optional
navigation line (이전/다음) has the agreed shape, overview pages stay free of
implementation vocabulary, dotori-docs never links into dev-docs, and prose is
written in the 합니다 register.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# The test service mounts the docs read-only at /workspace/dotori-docs; a
# source checkout has them next to app/.
_CANDIDATES = (
    Path("/workspace/dotori-docs"),
    Path(__file__).resolve().parents[3] / "dotori-docs",
)
DOCS_ROOT = next((path for path in _CANDIDATES if path.is_dir()), _CANDIDATES[-1])

OVERVIEW_FORBIDDEN = (
    "app/",
    "마이크로커널",
    "DDD",
    "aggregate",
    "Ports and Adapters",
    "Protocol",
)

_FENCE = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_NAV_LINE = re.compile(r"^> (이전|다음): \S")
_OLD_HEADER = re.compile(r"^> - (수준|읽을 사람|먼저 읽을 문서|다음에 읽을 문서):", re.M)
_PLAIN_REGISTER = re.compile(r"[가-힣](?<!니)다\.(\s|$)")


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text)


def _slug(heading: str) -> str:
    heading = heading.replace("`", "").strip().lower()
    return re.sub(r"[^\w\- ]", "", heading).replace(" ", "-")


def _anchors(text: str) -> set[str]:
    return {_slug(match.group(1)) for match in _HEADING.finditer(_strip_fences(text))}


def broken_links(path: Path, text: str) -> list[str]:
    problems = []
    for match in _LINK.finditer(_strip_fences(text)):
        link = match.group(1)
        if re.match(r"(https?:|mailto:)", link):
            continue
        target_part, _, fragment = link.partition("#")
        target = path if not target_part else (path.parent / target_part).resolve()
        if not target.exists():
            problems.append(f"{link}: target does not exist")
            continue
        if fragment and target.suffix == ".md":
            if _slug(fragment) not in _anchors(target.read_text(encoding="utf-8")):
                problems.append(f"{link}: no such heading")
    return problems


def navigation_problem(text: str) -> str | None:
    """A page may open with one `> 이전: ... · 다음: ...` line under the title."""
    if _OLD_HEADER.search(text):
        return "uses the retired four-line reader header; use one 이전/다음 line"
    lines = text.split("\n")
    if len(lines) > 2 and lines[2].startswith(">") and not _NAV_LINE.match(lines[2]):
        return "the quote under the title must be an 이전/다음 navigation line"
    return None


def plain_register_lines(text: str) -> list[str]:
    hits = []
    for line in _strip_fences(text).split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(("|", "#", ">", "```")):
            continue
        if _PLAIN_REGISTER.search(stripped):
            hits.append(stripped[:60])
    return hits


def _docs() -> list[Path]:
    return sorted(DOCS_ROOT.rglob("*.md"))


def _relative(path: Path) -> str:
    return path.relative_to(DOCS_ROOT).as_posix()


def test_docs_are_reachable_from_the_test_environment():
    assert DOCS_ROOT.is_dir(), (
        "The test service must mount ./dotori-docs read-only at /workspace/dotori-docs."
    )
    assert len(_docs()) >= 30


def test_every_relative_link_and_anchor_resolves():
    problems = [
        f"{_relative(path)}: {problem}"
        for path in _docs()
        for problem in broken_links(path, path.read_text(encoding="utf-8"))
    ]
    assert not problems, "\n".join(problems)


def test_navigation_lines_have_the_agreed_shape():
    problems = []
    for path in _docs():
        problem = navigation_problem(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        if problem:
            problems.append(f"{_relative(path)}: {problem}")
    assert not problems, "\n".join(problems)


def test_overview_pages_stay_free_of_implementation_vocabulary():
    problems = []
    for path in _docs():
        if not _relative(path).startswith("overview/"):
            continue
        text = path.read_text(encoding="utf-8")
        problems += [f"{_relative(path)}: {word}" for word in OVERVIEW_FORBIDDEN if word in text]
    assert not problems, "\n".join(problems)


def test_docs_never_link_into_dev_docs():
    problems = [
        f"{_relative(path)}: {link}"
        for path in _docs()
        for link in _LINK.findall(_strip_fences(path.read_text(encoding="utf-8")))
        if "dev-docs" in link
    ]
    assert not problems, "\n".join(problems)


def test_prose_uses_the_polite_register():
    problems = [
        f"{_relative(path)}: {line}"
        for path in _docs()
        for line in plain_register_lines(path.read_text(encoding="utf-8"))
    ]
    assert not problems, "\n".join(problems)


def test_checkers_detect_planted_violations(tmp_path):
    """Positive controls: a checker that never fires would pass the suite above."""
    page = tmp_path / "page.md"
    page.write_text("# 제목\n\n## 있는 제목\n", encoding="utf-8")

    bad = "[없는 파일](./missing.md) [없는 앵커](#없는-제목) [정상](#있는-제목)"
    assert len(broken_links(page, bad)) == 2

    assert navigation_problem("# 제목\n\n> - 수준: L2 구조\n") is not None
    assert navigation_problem("# 제목\n\n> 인용문\n") is not None
    assert navigation_problem("# 제목\n\n> 이전: [문서](./a.md) · 다음: [문서](./b.md)\n\n본문\n") is None
    assert navigation_problem("# 제목\n\n본문\n") is None

    assert plain_register_lines("이렇게 한다.\n\n이렇게 합니다.") == ["이렇게 한다."]
