from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .contracts import (
    CanonicalDocument,
    ChildChunk,
    ParseResult,
    ParsedBlock,
    ParsedBlockType,
    ParentChunk,
    SourceLocation,
)
from .token_chunking import (TokenSplitter, markdown_segments, split_markdown_table,
                             token_count, token_prefix, token_suffix)
from .text_normalization import normalize_search_text


@dataclass(frozen=True)
class Chunk:
    id: int
    content: str


DEFAULT_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "；", " ", "")
_FENCE = re.compile(r"(?ms)^(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^(?P=fence)\s*$")
_HEADING = re.compile(r"^#{1,6} .+$")


class RecursiveSplitter:
    """按字符数切分，优先保留语义边界；不依赖第三方 tokenizer。"""

    def __init__(self, chunk_size: int = 200, chunk_overlap: int = 50, separators=None):
        self.chunk_size = max(1, int(chunk_size))
        overlap = max(0, int(chunk_overlap))
        self.chunk_overlap = min(overlap, self.chunk_size - 1)
        self.separators = tuple(separators) if separators is not None else DEFAULT_SEPARATORS
        if not self.separators or self.separators[-1] != "":
            self.separators = self.separators + ("",)

    def split(self, text: str | None) -> list[Chunk]:
        if not text or not text.strip():
            return []
        pieces = self._protected_pieces(text)
        raw: list[str] = []
        for piece, atomic in pieces:
            raw.extend([piece] if atomic else self._recursive(piece, 0))
        raw = self._merge_small(raw)
        raw = self._join_headings(raw)
        result: list[Chunk] = []
        for i, value in enumerate(raw):
            if i and self.chunk_overlap:
                value = raw[i - 1][-self.chunk_overlap :] + value
            result.append(Chunk(i, value))
        return result

    def _protected_pieces(self, text: str):
        cursor = 0
        for match in _FENCE.finditer(text):
            if match.start() > cursor:
                yield text[cursor : match.start()], False
            yield match.group(0), True
            cursor = match.end()
        if cursor < len(text):
            yield text[cursor:], False

    def _recursive(self, text: str, level: int) -> list[str]:
        if len(text) <= self.chunk_size:
            return [text] if text.strip() else []
        separator = self.separators[min(level, len(self.separators) - 1)]
        if separator == "":
            return [text[i : i + self.chunk_size] for i in range(0, len(text), self.chunk_size)]
        if separator not in text:
            return self._recursive(text, level + 1)
        parts = text.split(separator)
        fragments = [parts[0]] + [separator + part for part in parts[1:]]
        out: list[str] = []
        for part in fragments:
            if len(part) <= self.chunk_size:
                if part.strip(): out.append(part)
            else:
                out.extend(self._recursive(part, level + 1))
        return out

    def _merge_small(self, parts: list[str]) -> list[str]:
        merged: list[str] = []
        for part in parts:
            if not part.strip():
                continue
            if merged and len(merged[-1]) + len(part) <= self.chunk_size:
                merged[-1] += part
            else:
                merged.append(part)
        return merged

    def _join_headings(self, parts: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(parts):
            current = parts[i].strip()
            if _HEADING.fullmatch(current) and i + 1 < len(parts):
                out.append(parts[i] + parts[i + 1]); i += 2
            else:
                out.append(parts[i]); i += 1
        return out


def _id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def _permission_metadata(document: CanonicalDocument) -> dict:
    """Copy the document ACL to every parent/child chunk."""
    source = document.metadata if isinstance(document.metadata, dict) else {}
    visibility = source.get("visibility")
    if not visibility:
        visibility = "customer_public" if document.classification == "public" else "internal"
    effective_until = source.get("effective_until")
    if effective_until is None and document.expires_at is not None:
        effective_until = document.expires_at.isoformat()
    return {
        "tenant_id": str(source.get("tenant_id") or document.business_unit_id),
        "business_unit_id": document.business_unit_id,
        "visibility": str(visibility),
        "allowed_roles": tuple(sorted(document.allowed_roles)),
        "classification": document.classification,
        "document_status": document.status.value,
        "product_skus": tuple(str(value) for value in source.get("product_skus") or ()),
        "product_models": tuple(str(value) for value in source.get("product_models") or ()),
        "document_type": str(source.get("document_type") or "product_document"),
        "document_version": str(source.get("document_version") or source.get("version")
                                or document.version),
        "logical_document_key": str(source.get("logical_document_key") or ""),
        "locale": str(source.get("locale") or document.language),
        "language": document.language,
        "version": str(source.get("version") or document.version),
        "effective_from": source.get("effective_from"),
        "effective_until": effective_until,
    }


PARENT_SPLITTER_VERSION = "parent-child-qwen-tokens-v2"


class ParentChildSplitter:
    version = PARENT_SPLITTER_VERSION

    def __init__(self, parent_tokens: int = 800, child_tokens: int = 200,
                 child_overlap_tokens: int = 20, *, parent_chars: int | None = None,
                 child_chars: int | None = None, overlap: int | None = None):
        # Legacy keyword aliases remain accepted; all sizes now count Qwen tokens.
        parent_tokens = parent_chars if parent_chars is not None else parent_tokens
        child_tokens = child_chars if child_chars is not None else child_tokens
        child_overlap_tokens = overlap if overlap is not None else child_overlap_tokens
        self.parent_splitter = TokenSplitter(parent_tokens, round(parent_tokens * .1))
        self.child_splitter = TokenSplitter(child_tokens, child_overlap_tokens)

    def split(self, doc: CanonicalDocument) -> tuple[list[ParentChunk], list[ChildChunk]]:
        image_rows = {int(row["image_index"]): row for row in doc.metadata.get("images", ())}
        if image_rows:
            parents, children = [], []
            for image_index in sorted(image_rows):
                row = image_rows[image_index]
                text = str(row.get("ocr_text", "")).strip()
                if not text:
                    continue
                location = f"image:{image_index}"
                for parent_part in self.parent_splitter.split(text):
                    parent_id = _id(doc.document_id, str(doc.version), location,
                                    str(parent_part.id), parent_part.content)
                    base = {
                        **_permission_metadata(doc),
                        "block_type": "image", "image_id": row["image_id"],
                        "image_index": image_index, "image_source_kind": row["source_kind"],
                        "page_number": row.get("page_number"),
                        "page_image_index": row["page_image_index"],
                        "ocr_confidence": row.get("ocr_confidence"),
                        "ocr_status": row["ocr_status"], "parent_id": parent_id,
                    }
                    parents.append(ParentChunk(parent_id, doc.document_id,
                                               parent_part.content, location,
                                               _id(parent_part.content), dict(base)))
                    for part in self.child_splitter.split(parent_part.content):
                        child_id = _id(parent_id, str(part.id), part.content)
                        children.append(ChildChunk(child_id, parent_id, doc.document_id,
                                                   part.content, location, _id(part.content),
                                                   dict(base)))
            return parents, children
        parents, children = [], []
        for segment, is_table in markdown_segments(doc.content):
            pieces = (split_markdown_table(segment, self.parent_splitter.chunk_size)
                      if is_table else [part.content for part in self.parent_splitter.split(segment)])
            for parent_text in pieces:
                pi = len(parents)
                pid = _id(doc.document_id, str(doc.version), str(pi), parent_text)
                base = _permission_metadata(doc)
                if is_table:
                    base["block_type"] = "table"
                parents.append(ParentChunk(
                    pid, doc.document_id, parent_text, doc.location,
                    _id(parent_text), dict(base),
                ))
                child_pieces = (split_markdown_table(parent_text, self.child_splitter.chunk_size)
                                if is_table else [part.content for part in self.child_splitter.split(parent_text)])
                for child_ordinal, child_text in enumerate(child_pieces):
                    cid = _id(pid, str(child_ordinal), child_text)
                    children.append(ChildChunk(
                        cid, pid, doc.document_id, child_text, doc.location,
                        _id(child_text), dict(base),
                    ))
        return parents, children


PDF_SPLITTER_VERSION = "pdf-aware-qwen-tokens-v4"
_PAGE_ARTIFACT = re.compile(
    r"^(?:page\s*)?\d+(?:\s*(?:/|of)\s*\d+)?$|^(?:copyright|©)\s*\d*\s*$",
    re.IGNORECASE,
)
_OCR_BOILERPLATE_LINE = re.compile(
    r"^(?:#{1,6}\s*)?(?:"
    r"未发现可检索的产品信息(?:[：:].*)?|"
    r"未发现(?:明确)?(?:可检索的)?(?:产品|技术|型号|参数)信息(?:[：:].*)?|"
    r"最终输出|输出结果|关系说明|关系说明如下|"
    r"图中标注、?部件、?箭头、?步骤、?指示灯状态(?:之间)?(?:明确展示的)?关系说明|"
    r"以上(?:内容|信息)(?:均)?(?:来自|基于)图片.*|"
    r"所有(?:文字|信息).*(?:直接辨认|未补充|未猜测).*"
    r")\s*[：:]?\s*$",
    re.IGNORECASE,
)
_OCR_SEPARATOR_LINE = re.compile(r"^[\s\-—_=*#]+$")
_CHAPTER_FOOTER = re.compile(r"^第\s*\d+\s*章\b|^chapter\s+\d+\b", re.IGNORECASE)
_REPEATED_DEVICE_ROW = re.compile(
    r"(?:实时网速[：:]\s*上行\s*\d+(?:\.\d+)?\s*(?:K|M)?B/s\s*"
    r"下行\s*\d+(?:\.\d+)?\s*(?:K|M)?B/s\s*管理\s*(?:禁用|启用)\s*"
    r"(?:匿名主机|未知设备)\s*){2,}",
    re.IGNORECASE,
)


def _collapse_repeated_ui_rows(value: str) -> str:
    """Collapse OCR/text-layer copies of the same device-list placeholder."""
    value = normalize_search_text(value)
    return _REPEATED_DEVICE_ROW.sub("实时网速：上行0KB/s 下行0KB/s 管理 禁用 匿名主机\n", value)


def _clean_ocr_text(value: str) -> str:
    """Remove vision-model narration while retaining observable product facts."""
    value = _collapse_repeated_ui_rows(value)
    output: list[str] = []
    for raw in value.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw.strip()
        if not line or _OCR_SEPARATOR_LINE.fullmatch(line):
            if output and output[-1]:
                output.append("")
            continue
        if _OCR_BOILERPLATE_LINE.fullmatch(line):
            continue
        # The prompt asks the vision model to emit this sentence when an image
        # has no useful information. It is control narration, not knowledge.
        if "未发现可检索的产品信息" in line:
            continue
        output.append(line)
    cleaned = "\n".join(output).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    meaningful = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", cleaned)
    if len(meaningful) < 6:
        return ""
    return cleaned


def _is_page_artifact(value: str) -> bool:
    text = value.strip()
    if _PAGE_ARTIFACT.fullmatch(text):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 3 and any(re.fullmatch(r"\d{1,4}", line) for line in lines):
        if any(_CHAPTER_FOOTER.search(line) for line in lines):
            return True
    return False


@dataclass(frozen=True)
class _PdfUnit:
    text: str
    location: SourceLocation
    ordinal: int
    image_index: int | None = None


def _full_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _location_label(location: SourceLocation) -> str:
    pages = (f"page:{location.page_start}" if location.page_start == location.page_end
             else f"pages:{location.page_start}-{location.page_end}")
    if not location.section_path:
        return pages
    return pages + "#" + " > ".join(location.section_path)


def _merge_location(units: list[_PdfUnit]) -> SourceLocation:
    first = units[0].location
    section = first.section_path
    if any(unit.location.section_path != section for unit in units):
        section = ()
    block_type = (first.block_type if all(unit.location.block_type == first.block_type for unit in units)
                  else ParsedBlockType.PARAGRAPH)
    return SourceLocation(
        min(unit.location.page_start for unit in units),
        max(unit.location.page_end for unit in units),
        section,
        block_type,
    )


class PdfAwareParentChildSplitter:
    """Deterministic structure-aware PDF splitter retaining exact page provenance."""

    version = PDF_SPLITTER_VERSION

    def __init__(self, parent_tokens: int = 800, child_tokens: int = 200,
                 child_overlap_tokens: int = 20, *, parent_chars: int | None = None,
                 child_chars: int | None = None, overlap: int | None = None):
        # Keep small explicit budgets usable for focused documents and tests.
        # Production defaults remain 800/200; silently raising a caller's
        # requested budget made short tables impossible to split at all.
        self.parent_tokens = max(16, int(parent_chars if parent_chars is not None else parent_tokens))
        self.child_tokens = max(8, int(child_chars if child_chars is not None else child_tokens))
        child_overlap = overlap if overlap is not None else child_overlap_tokens
        self.parent_overlap_tokens = round(self.parent_tokens * .1)
        self.parent_body_limit = self.parent_tokens - self.parent_overlap_tokens
        self.child_splitter = TokenSplitter(self.child_tokens, child_overlap)
        self.parent_splitter = TokenSplitter(self.parent_body_limit, 0)

    def split(self, document: CanonicalDocument, parsed: ParseResult, *,
              reviewed_complex_layout: bool = False) -> tuple[list[ParentChunk], list[ChildChunk]]:
        if not parsed.indexable and not reviewed_complex_layout:
            raise ValueError("pdf_parse_result_not_indexable")
        text_units = self._units(parsed.blocks)
        units = self._interleave_units(parsed, text_units)
        if not units:
            raise ValueError("pdf_chunks_empty")
        groups = self._merge_heading_only_groups(self._parent_groups(units))
        parents: list[ParentChunk] = []
        children: list[ChildChunk] = []
        for parent_ordinal, group in enumerate(groups):
            location = _merge_location(group)
            parent_text = "\n\n".join(unit.text for unit in group).strip()
            if parent_ordinal and group[0].location.block_type not in {
                    ParsedBlockType.TABLE, ParsedBlockType.IMAGE}:
                previous = groups[parent_ordinal - 1]
                if (previous[-1].location.block_type not in {
                        ParsedBlockType.TABLE, ParsedBlockType.IMAGE}
                        and previous[-1].location.section_path == group[0].location.section_path):
                    previous_text = "\n\n".join(unit.text for unit in previous)
                    budget = min(self.parent_overlap_tokens,
                                 max(0, self.parent_tokens - token_count(parent_text)
                                     - token_count("\n\n")))
                    overlap = token_suffix(previous_text, budget).strip()
                    while overlap and token_count(overlap + "\n\n" + parent_text) > self.parent_tokens:
                        budget -= 1
                        overlap = token_suffix(previous_text, budget).strip()
                    if overlap:
                        parent_text = overlap + "\n\n" + parent_text
            content_hash = _full_hash(parent_text)
            parent_id = _id(
                document.document_id,
                str(document.version),
                self.version,
                str(location.page_start),
                str(location.page_end),
                str(parent_ordinal),
                content_hash,
            )
            parent_metadata = self._metadata(document, location, parent_id)
            parent_metadata["parent_ordinal"] = parent_ordinal
            parents.append(ParentChunk(
                parent_id, document.document_id, parent_text, _location_label(location),
                content_hash, parent_metadata,
            ))
            children.extend(self._children(document, parent_id, group))
        if not children:
            raise ValueError("pdf_chunks_empty")
        return parents, children

    def preview(self, document: CanonicalDocument, parsed: ParseResult, *, limit: int = 20) -> dict:
        parents, children = self.split(document, parsed)
        cap = max(1, min(int(limit), 100))
        return {
            "splitter_version": self.version,
            "parent_count": len(parents),
            "child_count": len(children),
            "parents": [self._preview_row(parent.parent_id, parent.location, parent.text, parent.metadata)
                        for parent in parents[:cap]],
            "children": [self._preview_row(child.child_id, child.location, child.text, child.metadata)
                         for child in children[:cap]],
        }

    @staticmethod
    def _preview_row(chunk_id: str, location: str, text: str, metadata: dict) -> dict:
        return {
            "chunk_id": chunk_id,
            "location": location,
            "text": text[:240] + ("..." if len(text) > 240 else ""),
            "page_start": metadata["page_start"],
            "page_end": metadata["page_end"],
            "section_path": list(metadata["section_path"]),
            "block_type": metadata["block_type"],
            "parent_id": metadata["parent_id"],
        }

    def _units(self, blocks: tuple[ParsedBlock, ...]) -> list[_PdfUnit]:
        units: list[_PdfUnit] = []
        ordered = sorted(blocks, key=lambda item: item.ordinal)
        index = 0
        while index < len(ordered):
            block = ordered[index]
            text = _collapse_repeated_ui_rows(block.text).strip()
            if not text or _is_page_artifact(text):
                index += 1
                continue
            if block.location.block_type == ParsedBlockType.TABLE:
                table_parts = [text]
                next_index = index + 1
                while next_index < len(ordered):
                    following = ordered[next_index]
                    if (following.location.block_type != ParsedBlockType.TABLE
                            or following.location.page_start != block.location.page_start
                            or following.location.page_end != block.location.page_end
                            or following.location.bbox != block.location.bbox):
                        break
                    table_parts.append(normalize_search_text(following.text).strip())
                    next_index += 1
                text = "\n".join(part for part in table_parts if part)
                pieces = self._split_table(text, self.parent_body_limit)
                index = next_index
            else:
                pieces = [chunk.content.strip() for chunk in self.parent_splitter.split(text)]
                index += 1
            for part_index, piece in enumerate(pieces):
                if piece:
                    units.append(_PdfUnit(piece, block.location, block.ordinal * 10000 + part_index))
        return units

    def _image_units(self, parsed: ParseResult,
                     text_units: list[_PdfUnit]) -> list[_PdfUnit]:
        units: list[_PdfUnit] = []
        page_sections: dict[int, tuple[str, ...]] = {}
        for unit in text_units:
            if unit.location.section_path:
                page_sections[unit.location.page_start] = unit.location.section_path
        for image in parsed.images:
            if not image.ocr_text.strip() or image.ocr_status not in {"ready", "low_confidence"}:
                continue
            cleaned = _clean_ocr_text(image.ocr_text)
            if not cleaned:
                continue
            page = image.page_number or 1
            location = SourceLocation(
                page, page, page_sections.get(page, ()), ParsedBlockType.IMAGE,
            )
            for part in self.parent_splitter.split(cleaned):
                units.append(_PdfUnit(part.content, location,
                                      image.image_index * 10000 + part.id,
                                      image.image_index))
        return units

    def _interleave_units(self, parsed: ParseResult,
                          text_units: list[_PdfUnit]) -> list[_PdfUnit]:
        """Place OCR text after the body of its source page, before the next page."""
        image_units = self._image_units(parsed, text_units)
        text_by_page: dict[int, list[_PdfUnit]] = {}
        image_by_page: dict[int, list[_PdfUnit]] = {}
        for unit in text_units:
            text_by_page.setdefault(unit.location.page_start, []).append(unit)
        for unit in image_units:
            image_by_page.setdefault(unit.location.page_start, []).append(unit)
        output: list[_PdfUnit] = []
        pages = sorted(set(text_by_page) | set(image_by_page))
        for page in pages:
            output.extend(sorted(text_by_page.get(page, ()), key=lambda row: row.ordinal))
            output.extend(sorted(image_by_page.get(page, ()), key=lambda row: row.ordinal))
        return output

    def _parent_groups(self, units: list[_PdfUnit]) -> list[list[_PdfUnit]]:
        groups: list[list[_PdfUnit]] = []
        current: list[_PdfUnit] = []
        current_tokens = 0
        for unit in units:
            is_table = unit.location.block_type == ParsedBlockType.TABLE
            section_changed = bool(
                current and current[-1].location.section_path != unit.location.section_path
                and unit.location.section_path
            )
            page_gap = bool(current and unit.location.page_start > current[-1].location.page_end + 1)
            must_flush = bool(current and (
                is_table
                or current[-1].location.block_type == ParsedBlockType.TABLE
                or section_changed
                or page_gap
                or current_tokens + token_count("\n\n") + token_count(unit.text) > self.parent_body_limit
            ))
            if must_flush:
                groups.append(current)
                current = []
                current_tokens = 0
            current.append(unit)
            current_tokens += token_count(unit.text) + (token_count("\n\n") if current_tokens else 0)
            if is_table:
                groups.append(current)
                current = []
                current_tokens = 0
        if current:
            groups.append(current)
        return groups

    def _merge_heading_only_groups(self, groups: list[list[_PdfUnit]]) -> list[list[_PdfUnit]]:
        output: list[list[_PdfUnit]] = []
        pending: list[_PdfUnit] = []
        for group in groups:
            if all(unit.location.block_type == ParsedBlockType.HEADING for unit in group):
                pending.extend(group)
                continue
            if pending:
                candidate = pending + group
                if token_count("\n\n".join(unit.text for unit in candidate)) <= self.parent_body_limit:
                    group = candidate
                elif output:
                    previous = output[-1] + pending
                    if token_count("\n\n".join(unit.text for unit in previous)) <= self.parent_body_limit:
                        output[-1] = previous
                    else:
                        output.append(pending)
                else:
                    output.append(pending)
                pending = []
            output.append(group)
        if pending:
            if output:
                candidate = output[-1] + pending
                if token_count("\n\n".join(unit.text for unit in candidate)) <= self.parent_body_limit:
                    output[-1] = candidate
                else:
                    output.append(pending)
            else:
                output.append(pending)
        return output

    def _children(self, document: CanonicalDocument, parent_id: str,
                  group: list[_PdfUnit]) -> list[ChildChunk]:
        output: list[ChildChunk] = []
        heading = ""
        for unit in group:
            if unit.location.block_type == ParsedBlockType.HEADING:
                heading = unit.text.strip()
                continue
            if unit.location.block_type == ParsedBlockType.TABLE:
                pieces = self._split_table(unit.text, self.child_tokens)
            else:
                pieces = [chunk.content.strip() for chunk in self.child_splitter.split(unit.text)]
            for part_index, piece in enumerate(pieces):
                text = self._with_heading(heading, piece)
                text = self._with_document_context(document, text)
                content_hash = _full_hash(text)
                child_id = _id(
                    parent_id,
                    self.version,
                    str(unit.location.page_start),
                    str(unit.location.page_end),
                    str(unit.ordinal),
                    str(part_index),
                    content_hash,
                )
                metadata = self._metadata(document, unit.location, parent_id)
                metadata.update({"block_ordinal": unit.ordinal, "child_ordinal": part_index})
                if unit.location.block_type == ParsedBlockType.IMAGE:
                    if unit.image_index is None:
                        raise RuntimeError("pdf_image_index_missing")
                    image_index = unit.image_index
                    image = next(image for image in document.metadata.get("images", ())
                                 if int(image.get("image_index", 0)) == image_index)
                    metadata.update({
                        "image_id": image["image_id"], "image_index": image_index,
                        "image_source_kind": image["source_kind"],
                        "page_number": image.get("page_number"),
                        "page_image_index": image["page_image_index"],
                        "ocr_confidence": image.get("ocr_confidence"),
                        "ocr_status": image["ocr_status"],
                    })
                output.append(ChildChunk(
                    child_id, parent_id, document.document_id, text,
                    _location_label(unit.location), content_hash, metadata,
                ))
        if output:
            merged = self._merge_short_children(parent_id, output)
            deduplicated: list[ChildChunk] = []
            seen_keys: set[tuple[str, str | None]] = set()
            for row in merged:
                # Equal OCR text from two separate images is still separate
                # evidence: each image has its own page and citation identity.
                image_id = (str(row.metadata.get("image_id"))
                            if row.metadata.get("block_type") == "image" else None)
                key = (row.text, image_id)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduplicated.append(row)
            return deduplicated
        location = _merge_location(group)
        text = self._with_document_context(
            document, "\n\n".join(unit.text for unit in group).strip(),
        )
        content_hash = _full_hash(text)
        child_id = _id(parent_id, self.version, "heading-only", content_hash)
        return [ChildChunk(
            child_id, parent_id, document.document_id, text, _location_label(location),
            content_hash, self._metadata(document, location, parent_id),
        )]

    def _merge_short_children(self, parent_id: str,
                              rows: list[ChildChunk]) -> list[ChildChunk]:
        """Merge tiny adjacent children when their provenance is identical."""
        merged: list[ChildChunk] = []
        minimum = min(40, max(12, self.child_tokens // 5))
        for row in rows:
            if not merged:
                merged.append(row)
                continue
            previous = merged[-1]
            previous_type = previous.metadata.get("block_type")
            row_type = row.metadata.get("block_type")
            same_scope = previous.location == row.location
            same_location = same_scope and (
                (previous_type == row_type == "image"
                 and previous.metadata.get("image_id") == row.metadata.get("image_id"))
                or (previous_type not in {"image", "table"}
                    and row_type not in {"image", "table"})
            )
            combined = previous.text.rstrip() + "\n" + row.text.lstrip()
            if (same_location
                    and (token_count(previous.text) < minimum or token_count(row.text) < minimum)
                    and token_count(combined) <= self.child_tokens):
                content_hash = _full_hash(combined)
                metadata = dict(previous.metadata)
                metadata["merged_short_child"] = True
                child_id = _id(parent_id, self.version, "merged", previous.child_id,
                               row.child_id, content_hash)
                merged[-1] = ChildChunk(
                    child_id, parent_id, previous.document_id, combined,
                    previous.location, content_hash, metadata,
                )
            else:
                merged.append(row)
        return merged

    def _metadata(self, document: CanonicalDocument, location: SourceLocation,
                  parent_id: str) -> dict:
        return {
            **_permission_metadata(document),
            "page_start": location.page_start,
            "page_end": location.page_end,
            "section_path": tuple(location.section_path),
            "block_type": location.block_type.value,
            "language": document.language,
            "parent_id": parent_id,
            "source_hash": str(document.metadata.get("source_hash", "")),
            "splitter_version": self.version,
        }

    def _with_heading(self, heading: str, text: str) -> str:
        heading = heading.strip()
        if not heading or text.startswith(heading):
            return text
        prefix = token_prefix(heading, min(40, self.child_tokens // 4)) + "\n"
        available = max(1, self.child_tokens - token_count(prefix))
        return prefix + token_prefix(text, available)

    def _with_document_context(self, document: CanonicalDocument, text: str) -> str:
        metadata = document.metadata if isinstance(document.metadata, dict) else {}
        models = [str(value).strip() for value in metadata.get("product_models") or ()
                  if str(value).strip()]
        if not models:
            return token_prefix(text, self.child_tokens)
        prefix = f"产品型号：{', '.join(models[:4])}\n"
        if text.startswith(prefix):
            return token_prefix(text, self.child_tokens)
        available = max(1, self.child_tokens - token_count(prefix))
        return prefix + token_prefix(text, available)

    def _split_table(self, text: str, limit: int) -> list[str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return []
        if len(lines) == 1:
            return [row.content for row in TokenSplitter(limit).split(lines[0])]
        column_header = next((index for index, line in enumerate(lines) if "|" in line), 0)
        header_count = column_header + 1
        if len(lines) > header_count and re.fullmatch(r"[|:+\-\s]+", lines[header_count]):
            header_count += 1
        header = "\n".join(lines[:header_count])
        if token_count(header) >= limit:
            return [row.content for row in TokenSplitter(limit).split(text)]
        rows = lines[header_count:]
        if not rows:
            return [header]
        chunks: list[str] = []
        current = header
        for row in rows:
            candidate = current + "\n" + row
            if token_count(candidate) <= limit or current == header:
                if token_count(candidate) <= limit:
                    current = candidate
                    continue
                room = max(1, limit - token_count(header + "\n"))
                for part in TokenSplitter(room).split(row):
                    chunks.append(header + "\n" + part.content)
                current = header
                continue
            chunks.append(current)
            current = header + "\n" + row
        if current != header:
            chunks.append(current)
        return chunks or [header]
