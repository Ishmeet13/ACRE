"""
AST Parser
==========
Uses tree-sitter to parse source files and extract semantic chunks at the
function / class / method level — not naive line-based splits.

Each chunk carries:
  - code text
  - chunk_type: function | class | method | module_level
  - name: symbol name
  - start_line / end_line
  - file_path (relative to repo root)
  - language
  - docstring (if present)
  - complexity_score (rough cyclomatic estimate)
  - imports: list of import statements in scope
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# tree-sitter ≥ 0.22 unified API
try:
    import tree_sitter_python as tspython
    import tree_sitter_javascript as tsjavascript
    import tree_sitter_typescript as tstypescript
    import tree_sitter_go as tsgo
    import tree_sitter_java as tsjava
    import tree_sitter_rust as tsrust
    from tree_sitter import Language, Parser
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False

LANGUAGE_MAP = {
    ".py":    ("python",     lambda: Language(tspython.language()) if HAS_TREE_SITTER else None),
    ".js":    ("javascript", lambda: Language(tsjavascript.language()) if HAS_TREE_SITTER else None),
    ".ts":    ("typescript", lambda: Language(tstypescript.language_typescript()) if HAS_TREE_SITTER else None),
    ".tsx":   ("tsx",        lambda: Language(tstypescript.language_tsx()) if HAS_TREE_SITTER else None),
    ".go":    ("go",         lambda: Language(tsgo.language()) if HAS_TREE_SITTER else None),
    ".java":  ("java",       lambda: Language(tsjava.language()) if HAS_TREE_SITTER else None),
    ".rs":    ("rust",       lambda: Language(tsrust.language()) if HAS_TREE_SITTER else None),
}

# Node types that represent top-level symbols worth chunking
CHUNK_NODE_TYPES = {
    "python":     {"function_definition", "async_function_definition", "class_definition"},
    "javascript": {"function_declaration", "arrow_function", "class_declaration", "method_definition"},
    "typescript": {"function_declaration", "arrow_function", "class_declaration", "method_definition"},
    "go":         {"function_declaration", "method_declaration", "type_declaration"},
    "java":       {"method_declaration", "class_declaration", "interface_declaration"},
    "rust":       {"function_item", "impl_item", "struct_item", "enum_item"},
}


@dataclass
class CodeChunk:
    chunk_id: str
    file_path: str          # relative to repo root
    language: str
    chunk_type: str         # function | class | method | module_level
    name: str
    code: str
    start_line: int
    end_line: int
    docstring: str = ""
    complexity_score: int = 0
    imports: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def document_text(self) -> str:
        """Text that gets embedded — enriched with context headers."""
        header = (
            f"File: {self.file_path}\n"
            f"Language: {self.language}\n"
            f"Symbol: {self.chunk_type} `{self.name}`\n"
            f"Lines: {self.start_line}–{self.end_line}\n"
            "---\n"
        )
        return header + self.code


class ASTParser:
    """Parse source files using tree-sitter; fall back to regex chunking."""

    def __init__(self):
        self._parsers: dict[str, Parser] = {}

    def _get_parser(self, ext: str) -> Optional[Parser]:
        if not HAS_TREE_SITTER or ext not in LANGUAGE_MAP:
            return None
        if ext not in self._parsers:
            try:
                lang = LANGUAGE_MAP[ext][1]()
                p = Parser(lang)
                self._parsers[ext] = p
            except Exception:
                return None
        return self._parsers.get(ext)

    # ── Public API ────────────────────────────────────────────────────────────
    def parse_file(self, abs_path: str, repo_root: str) -> list[dict]:
        rel_path = str(Path(abs_path).relative_to(repo_root))
        ext = Path(abs_path).suffix.lower()
        lang_name = LANGUAGE_MAP.get(ext, (ext.lstrip("."), None))[0]

        try:
            source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        imports = _extract_imports(source, lang_name)
        parser = self._get_parser(ext)

        if parser is not None:
            chunks = self._ts_parse(source, parser, lang_name, rel_path, imports)
        else:
            chunks = self._regex_fallback(source, lang_name, rel_path, imports)

        # Always include a module-level chunk with all imports + file header
        module_chunk = _make_module_chunk(source, rel_path, lang_name, imports)
        return [module_chunk.to_dict()] + [c.to_dict() for c in chunks]

    # ── tree-sitter path ──────────────────────────────────────────────────────
    def _ts_parse(self, source: str, parser: Parser, lang: str,
                  rel_path: str, imports: list[str]) -> list[CodeChunk]:
        tree = parser.parse(source.encode())
        node_types = CHUNK_NODE_TYPES.get(lang, set())
        lines = source.splitlines()
        chunks: list[CodeChunk] = []

        def walk(node, depth=0):
            if node.type in node_types:
                name = _extract_name(node, source)
                code = source[node.start_byte:node.end_byte]
                start_line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                chunk_type = _node_to_chunk_type(node.type, lang)
                docstring = _extract_docstring(node, source, lang)
                complexity = _estimate_complexity(code, lang)

                chunk = CodeChunk(
                    chunk_id=_make_chunk_id(rel_path, start_line),
                    file_path=rel_path,
                    language=lang,
                    chunk_type=chunk_type,
                    name=name,
                    code=code,
                    start_line=start_line,
                    end_line=end_line,
                    docstring=docstring,
                    complexity_score=complexity,
                    imports=imports,
                )
                chunks.append(chunk)
                # recurse into class bodies for methods
                if chunk_type == "class":
                    for child in node.children:
                        walk(child, depth + 1)
            else:
                for child in node.children:
                    walk(child, depth + 1)

        walk(tree.root_node)
        return chunks

    # ── Regex fallback (when tree-sitter grammar unavailable) ─────────────────
    def _regex_fallback(self, source: str, lang: str,
                        rel_path: str, imports: list[str]) -> list[CodeChunk]:
        patterns = {
            "python": r"^(async )?def ([A-Za-z_]\w*)\s*\(",
            "javascript": r"^(async )?function ([A-Za-z_]\w*)\s*\(",
            "java": r"^\s*(public|private|protected).*\s([A-Za-z_]\w*)\s*\(",
        }
        pat = patterns.get(lang, r"^(def|func|function|fn) ([A-Za-z_]\w*)")
        lines = source.splitlines()
        chunks = []
        i = 0
        while i < len(lines):
            m = re.match(pat, lines[i])
            if m:
                name = m.group(2) if m.lastindex >= 2 else m.group(0)
                start = i
                # simple heuristic: find end by indentation drop
                end = i + 1
                while end < len(lines) and (
                    not lines[end].strip() or lines[end][0] in (" ", "\t")
                ):
                    end += 1
                code = "\n".join(lines[start:end])
                chunks.append(CodeChunk(
                    chunk_id=_make_chunk_id(rel_path, start + 1),
                    file_path=rel_path,
                    language=lang,
                    chunk_type="function",
                    name=name,
                    code=code,
                    start_line=start + 1,
                    end_line=end,
                    imports=imports,
                    complexity_score=_estimate_complexity(code, lang),
                ))
                i = end
            else:
                i += 1
        return chunks


# ── Helpers ───────────────────────────────────────────────────────────────────
def _make_chunk_id(rel_path: str, start_line: int) -> str:
    import hashlib
    raw = f"{rel_path}:{start_line}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _extract_name(node, source: str) -> str:
    for child in node.children:
        if child.type in {"identifier", "type_identifier", "field_identifier"}:
            return source[child.start_byte:child.end_byte]
    return "anonymous"


def _node_to_chunk_type(node_type: str, lang: str) -> str:
    if "class" in node_type or "impl" in node_type or "struct" in node_type:
        return "class"
    if "method" in node_type:
        return "method"
    return "function"


def _extract_docstring(node, source: str, lang: str) -> str:
    if lang == "python":
        for child in node.children:
            if child.type == "block":
                for stmt in child.children:
                    if stmt.type == "expression_statement":
                        for sub in stmt.children:
                            if sub.type == "string":
                                raw = source[sub.start_byte:sub.end_byte]
                                return raw.strip('"\' \n').strip()
    return ""


def _estimate_complexity(code: str, lang: str) -> int:
    """Rough cyclomatic complexity: count branching keywords."""
    keywords = {"if", "elif", "else", "for", "while", "except", "case",
                "&&", "||", "?", "catch"}
    count = 1
    for kw in keywords:
        count += code.count(f" {kw} ") + code.count(f"\n{kw} ")
    return min(count, 50)


def _extract_imports(source: str, lang: str) -> list[str]:
    lines = source.splitlines()
    imports = []
    for line in lines[:60]:  # imports are always near top
        stripped = line.strip()
        if lang == "python" and (stripped.startswith("import ") or stripped.startswith("from ")):
            imports.append(stripped)
        elif lang in ("javascript", "typescript") and stripped.startswith("import "):
            imports.append(stripped)
        elif lang == "go" and stripped.startswith("import"):
            imports.append(stripped)
    return imports[:20]  # cap at 20


def _make_module_chunk(source: str, rel_path: str, lang: str,
                       imports: list[str]) -> CodeChunk:
    header_lines = source.splitlines()[:30]
    return CodeChunk(
        chunk_id=_make_chunk_id(rel_path, 0),
        file_path=rel_path,
        language=lang,
        chunk_type="module_level",
        name=Path(rel_path).stem,
        code="\n".join(header_lines),
        start_line=1,
        end_line=min(30, len(source.splitlines())),
        imports=imports,
        complexity_score=0,
    )
