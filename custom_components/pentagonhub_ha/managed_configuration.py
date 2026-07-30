"""Managed Home Assistant configuration runtime overlay."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any


RUNTIME_STATE_PATH = Path(".pentagonhub_ha") / "managed_runtime.json"
MANAGED_SOURCE_PATH = (
    Path(".pentagonhub_ha") / "managed-source" / "configuration.yaml"
)
PROXY_MARKER_BEGIN = "# PentagonHub managed reverse proxy: begin"
PROXY_MARKER_END = "# PentagonHub managed reverse proxy: end"
INTEGRATION_MARKER_BEGIN = "# PentagonHub managed integration: begin"
INTEGRATION_MARKER_END = "# PentagonHub managed integration: end"

_TOP_LEVEL_HTTP_RE = re.compile(r"^http\s*:\s*(?:#.*)?$")
_ANY_TOP_LEVEL_HTTP_RE = re.compile(r"^http\s*:")
_TOP_LEVEL_INTEGRATION_RE = re.compile(r"^pentagonhub_ha\s*:\s*(?:#.*)?$")
_HTTP_RUNTIME_KEY_RE = re.compile(r"^(use_x_forwarded_for|trusted_proxies)\s*:")


def canonical_managed_configuration(content: bytes) -> bytes:
    """Remove Worker-owned runtime markers from managed configuration bytes."""

    text = _decode(content)
    lines, newline, final_newline = _split_text(text)
    lines = _remove_marked_blocks(lines, PROXY_MARKER_BEGIN, PROXY_MARKER_END)
    lines = _remove_marked_blocks(
        lines,
        INTEGRATION_MARKER_BEGIN,
        INTEGRATION_MARKER_END,
    )
    return _join_text(lines, newline, final_newline).encode("utf-8")


def materialize_managed_configuration(config_dir: Path, content: bytes) -> bytes:
    """Apply the managed-host runtime overlay to source configuration bytes."""

    runtime = _read_runtime_state(config_dir)
    if runtime is None:
        return content

    canonical = canonical_managed_configuration(content)
    text = _decode(canonical)
    lines, newline, final_newline = _split_text(text)
    trusted_proxies = runtime["trusted_proxies"]

    if trusted_proxies:
        http_block = _find_top_level_block(lines, _TOP_LEVEL_HTTP_RE)
        if http_block is None and any(
            _ANY_TOP_LEVEL_HTTP_RE.match(line) for line in lines
        ):
            raise ValueError(
                "Managed Home Assistant http configuration must use YAML block form"
            )
        if http_block is not None:
            lines = _remove_http_runtime_keys(lines, http_block)
            http_block = _find_top_level_block(lines, _TOP_LEVEL_HTTP_RE)
        lines = _insert_proxy_runtime(lines, http_block, trusted_proxies)

    if runtime["ensure_pentagonhub_ha"] and not any(
        _TOP_LEVEL_INTEGRATION_RE.fullmatch(line) for line in lines
    ):
        lines.extend(
            [
                INTEGRATION_MARKER_BEGIN,
                "pentagonhub_ha:",
                INTEGRATION_MARKER_END,
            ]
        )

    return _join_text(lines, newline, final_newline).encode("utf-8")


def managed_file_bytes(relative_path: str, path: Path) -> bytes:
    """Read canonical bytes for a managed file."""

    content = path.read_bytes()
    if relative_path == "configuration.yaml":
        source_path = path.parent / MANAGED_SOURCE_PATH
        if (
            source_path.exists()
            and source_path.is_file()
            and not source_path.is_symlink()
        ):
            source = source_path.read_bytes()
            if materialize_managed_configuration(path.parent, source) == content:
                return source
        return canonical_managed_configuration(content)
    return content


def managed_file_hash(relative_path: str, path: Path) -> str | None:
    """Hash canonical managed bytes without Worker-owned runtime data."""

    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(managed_file_bytes(relative_path, path)).hexdigest()


def write_managed_file(config_dir: Path, relative_path: str, content: bytes) -> None:
    """Write source bytes while materializing managed-host runtime data."""

    target_path = config_dir / relative_path
    if relative_path == "configuration.yaml":
        source = content
        content = materialize_managed_configuration(config_dir, source)
        if (config_dir / RUNTIME_STATE_PATH).exists():
            source_path = config_dir / MANAGED_SOURCE_PATH
            source_path.parent.mkdir(parents=True, exist_ok=True)
            if source_path.parent.is_symlink() or source_path.is_symlink():
                raise ValueError("Managed configuration source path is unsafe")
            source_path.write_bytes(source)
    target_path.write_bytes(content)


def delete_managed_file(config_dir: Path, relative_path: str) -> bool:
    """Delete managed content while retaining required runtime configuration."""

    target_path = config_dir / relative_path
    if (
        not target_path.exists()
        or not target_path.is_file()
        or target_path.is_symlink()
    ):
        return False
    if relative_path == "configuration.yaml":
        source_path = config_dir / MANAGED_SOURCE_PATH
        if (
            source_path.exists()
            and source_path.is_file()
            and not source_path.is_symlink()
        ):
            source_path.unlink()
        runtime_only = materialize_managed_configuration(config_dir, b"")
        if runtime_only:
            target_path.write_bytes(runtime_only)
            return True
    target_path.unlink()
    return True


def _read_runtime_state(config_dir: Path) -> dict[str, Any] | None:
    path = config_dir / RUNTIME_STATE_PATH
    if path.is_symlink():
        raise ValueError("Managed runtime configuration is invalid")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as err:
        raise ValueError("Managed runtime configuration is invalid") from err

    if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
        raise ValueError("Managed runtime configuration is invalid")
    configuration = parsed.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("Managed runtime configuration is invalid")
    raw_proxies = configuration.get("trusted_proxies", [])
    if not isinstance(raw_proxies, list) or len(raw_proxies) > 32:
        raise ValueError("Managed runtime trusted proxies are invalid")
    proxies: list[str] = []
    for raw_proxy in raw_proxies:
        if (
            not isinstance(raw_proxy, str)
            or not raw_proxy
            or len(raw_proxy) > 128
            or "\n" in raw_proxy
            or "\r" in raw_proxy
        ):
            raise ValueError("Managed runtime trusted proxies are invalid")
        proxies.append(raw_proxy)
    return {
        "trusted_proxies": proxies,
        "ensure_pentagonhub_ha": configuration.get("ensure_pentagonhub_ha") is True,
    }


def _insert_proxy_runtime(
    lines: list[str],
    http_block: tuple[int, int] | None,
    trusted_proxies: list[str],
) -> list[str]:
    if http_block is None:
        runtime_lines = [
            PROXY_MARKER_BEGIN,
            "http:",
            "  use_x_forwarded_for: true",
            "  trusted_proxies:",
            *(f"    - {proxy}" for proxy in trusted_proxies),
            PROXY_MARKER_END,
        ]
        return [*lines, *runtime_lines]

    start, end = http_block
    indent = _child_indent(lines, start, end)
    runtime_lines = [
        f"{indent}{PROXY_MARKER_BEGIN}",
        f"{indent}use_x_forwarded_for: true",
        f"{indent}trusted_proxies:",
        *(f"{indent}  - {proxy}" for proxy in trusted_proxies),
        f"{indent}{PROXY_MARKER_END}",
    ]
    return [*lines[: start + 1], *runtime_lines, *lines[start + 1 :]]


def _remove_http_runtime_keys(
    lines: list[str],
    block: tuple[int, int],
) -> list[str]:
    start, end = block
    indent = _child_indent(lines, start, end)
    indent_length = len(indent)
    ranges: list[tuple[int, int]] = []
    index = start + 1
    while index < end:
        line = lines[index]
        is_direct_child = line.startswith(indent) and not line.startswith(
            f"{indent} "
        )
        if not is_direct_child or not _HTTP_RUNTIME_KEY_RE.match(
            line[indent_length:]
        ):
            index += 1
            continue
        finish = index + 1
        while finish < end:
            candidate = lines[finish]
            if (
                candidate.strip()
                and len(candidate) - len(candidate.lstrip(" ")) <= indent_length
            ):
                break
            finish += 1
        ranges.append((index, finish))
        index = finish

    result = list(lines)
    for range_start, range_end in reversed(ranges):
        del result[range_start:range_end]
    return result


def _child_indent(lines: list[str], start: int, end: int) -> str:
    indents = [
        len(line) - len(line.lstrip(" "))
        for line in lines[start + 1 : end]
        if line.strip() and not line.lstrip().startswith("#") and line.startswith(" ")
    ]
    return " " * min(indents) if indents else "  "


def _find_top_level_block(
    lines: list[str],
    pattern: re.Pattern[str],
) -> tuple[int, int] | None:
    for index, line in enumerate(lines):
        if not pattern.fullmatch(line):
            continue
        end = len(lines)
        for candidate in range(index + 1, len(lines)):
            value = lines[candidate]
            if value.strip() and not value.startswith((" ", "\t", "#")):
                end = candidate
                break
        return index, end
    return None


def _remove_marked_blocks(lines: list[str], begin: str, end: str) -> list[str]:
    result = list(lines)
    while True:
        start = next(
            (index for index, line in enumerate(result) if line.strip() == begin),
            None,
        )
        if start is None:
            return result
        finish = next(
            (
                index
                for index in range(start + 1, len(result))
                if result[index].strip() == end
            ),
            None,
        )
        if finish is None:
            raise ValueError(f"Managed configuration marker is incomplete: {begin}")
        del result[start : finish + 1]


def _split_text(text: str) -> tuple[list[str], str, bool]:
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.splitlines(), newline, text.endswith(("\n", "\r"))


def _join_text(lines: list[str], newline: str, final_newline: bool) -> str:
    joined = newline.join(lines)
    return f"{joined}{newline}" if final_newline and lines else joined


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ValueError("configuration.yaml must be UTF-8") from err
