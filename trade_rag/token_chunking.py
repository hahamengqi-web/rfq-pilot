"""Token windows using the Qwen3 Embedding tokenizer bundled with this project."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

from tokenizers import Tokenizer


_TOKENIZER_FILE = Path(__file__).parent / "assets" / "qwen3_embedding_tokenizer.json"


@lru_cache(maxsize=1)
def qwen_tokenizer() -> Tokenizer:
    return Tokenizer.from_file(str(_TOKENIZER_FILE))


def token_count(text: str) -> int:
    return len(qwen_tokenizer().encode(text, add_special_tokens=False).ids)


def token_prefix(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    offsets = qwen_tokenizer().encode(text, add_special_tokens=False).offsets
    if len(offsets) <= limit:
        return text
    end = min(limit, len(offsets))
    piece = text[:offsets[end - 1][1]]
    while end > 1 and token_count(piece) > limit:
        end -= 1
        piece = text[:offsets[end - 1][1]]
    return piece


def token_suffix(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    offsets = qwen_tokenizer().encode(text, add_special_tokens=False).offsets
    if len(offsets) <= limit:
        return text
    return text[offsets[-limit][0]:]


@dataclass(frozen=True)
class TokenChunk:
    id: int
    content: str


class TokenSplitter:
    def __init__(self, chunk_size: int, chunk_overlap: int = 0):
        self.chunk_size = max(1, int(chunk_size))
        self.chunk_overlap = min(max(0, int(chunk_overlap)), self.chunk_size - 1)

    def split(self, text: str | None) -> list[TokenChunk]:
        if not text or not text.strip():
            return []
        offsets = qwen_tokenizer().encode(text, add_special_tokens=False).offsets
        if not offsets:
            return []
        output = []
        start = 0
        step = self.chunk_size - self.chunk_overlap
        while start < len(offsets):
            end = min(start + self.chunk_size, len(offsets))
            piece = text[offsets[start][0]:offsets[end - 1][1]].strip()
            while token_count(piece) > self.chunk_size and end > start + 1:
                end -= 1
                piece = text[offsets[start][0]:offsets[end - 1][1]].strip()
            if piece:
                output.append(TokenChunk(len(output), piece))
            if end == len(offsets):
                break
            start += min(step, end - start)
        return output


_MARKDOWN_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")


def markdown_segments(text: str) -> list[tuple[str, bool]]:
    """Keep Markdown tables separate from surrounding prose before chunking."""
    lines = text.splitlines(keepends=True)
    output: list[tuple[str, bool]] = []
    body: list[str] = []
    index = 0
    while index < len(lines):
        if (index + 1 < len(lines) and "|" in lines[index]
                and _MARKDOWN_SEPARATOR.fullmatch(lines[index + 1].strip())):
            if body:
                output.append(("".join(body), False))
                body = []
            table = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                table.append(lines[index])
                index += 1
            output.append(("".join(table), True))
            continue
        body.append(lines[index])
        index += 1
    if body:
        output.append(("".join(body), False))
    return output


def split_markdown_table(text: str, limit: int) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    if len(lines) < 2 or not _MARKDOWN_SEPARATOR.fullmatch(lines[1]):
        return [chunk.content for chunk in TokenSplitter(limit).split(text)]
    header = "\n".join(lines[:2])
    if token_count(header) >= limit:
        return [chunk.content for chunk in TokenSplitter(limit).split(text)]
    output: list[str] = []
    current = header
    for row in lines[2:]:
        candidate = current + "\n" + row
        if token_count(candidate) <= limit:
            current = candidate
            continue
        if current != header:
            output.append(current)
        room = max(1, limit - token_count(header + "\n"))
        if token_count(row) > room:
            output.extend(header + "\n" + part.content
                          for part in TokenSplitter(room).split(row))
            current = header
        else:
            current = header + "\n" + row
    if current != header or not output:
        output.append(current)
    return output
