"""代码分块（第二期 RAG）。

代码 RAG 和文档 RAG 的核心区别就在分块：
  - 文档可按固定字符切（语义连续）
  - 代码必须按语义单元（函数/类）切，否则一个函数被切成两半，检索召回也用不了

策略：
  - Python：用 ast 按函数/类/方法切，保留完整语义单元
  - js/ts/go/java/rs/c/cpp/rb 等：用 tree-sitter 按函数/类切（见 tree_chunker.py），
    未安装 tree-sitter 依赖时自动回退滑窗，行为零回归
  - yaml/json/md/sh 等无语法树的语言：滑窗兜底，按行切 + overlap
  - 超长块（> MAX_CHUNK_LINES）：滑窗二次切分，带 overlap 保证边界语义连续
  - 每个块带元数据：file / type / name / start_line / end_line，存入 Qdrant payload

分块粒度的权衡：
  - 太小（如单行）：语义不完整，检索命中也看不懂上下文
  - 太大（如整文件）：向量稀释，相似度不准
  - 函数级是业界共识的甜点区
"""

from __future__ import annotations

import ast
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 单块最大行数。超过则滑窗二次切分。
# 50 行约 400-600 token，是代码块 embedding 的合理粒度。
MAX_CHUNK_LINES = 50

# 滑窗 overlap 行数，保证边界语义连续。
# 10 行能覆盖大多数函数的签名+前几行，跨块时上下文不丢。
SLIDING_OVERLAP = 10

# 用标准库 ast 分块的语言。其余代码语言的 AST 分块见 tree_chunker.py（tree-sitter）。
AST_SUPPORTED = {".py"}

# 跳过这些目录（和 tools/base.py 的 SKIP_DIRS 一致）
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}

# 只索引这些扩展名（避免把二进制/大文件塞进索引）
INDEXABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
    ".scala", ".sh", ".yaml", ".yml", ".json", ".toml", ".md",
}


@dataclass
class CodeChunk:
    """一个代码块。"""

    file: str           # 相对项目根的路径
    type: str           # FunctionDef / ClassDef / AsyncFunctionDef / block（滑窗块）
    name: str           # 函数名/类名；滑窗块为 f"L{start}-L{end}"
    start_line: int
    end_line: int
    code: str           # 块的源码
    content_hash: str   # code 的 md5，增量索引用

    @property
    def id(self) -> str:
        """稳定点 ID：file:name:start_line 的 hash。

        同一个函数改了内容但位置没变 → ID 不变 → upsert 覆盖。
        这是 Qdrant 增量更新的关键。
        """
        raw = f"{self.file}:{self.name}:{self.start_line}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()


def chunk_file(file_path: Path, project_root: Path) -> list[CodeChunk]:
    """对单个文件分块。

    返回 CodeChunk 列表。文件读失败返回空列表（不抛异常，索引容错）。
    """
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    rel_path = str(file_path.relative_to(project_root)).replace("\\", "/")

    suffix = file_path.suffix.lower()
    if suffix == ".py":
        chunks = _chunk_python(source, rel_path)
    else:
        chunks = _chunk_other(source, suffix, rel_path)

    return chunks


def _chunk_other(source: str, suffix: str, rel_path: str) -> list[CodeChunk]:
    """非 Python 代码文件：优先 tree-sitter 语义分块，失败/不支持则滑窗兜底。

    tree-sitter 是可选依赖：未安装时 try_chunk 返回 None，这里静默回退，
    索引流程永不中断（与 embedding 依赖缺失时的降级策略一致）。
    """
    try:
        from codebot.rag import tree_chunker

        chunks = tree_chunker.try_chunk(source, suffix, rel_path)
        if chunks is not None:
            return chunks
    except Exception as e:  # pragma: no cover - 防御性兜底，任何异常都不阻塞索引
        log.debug("tree-sitter 分块失败，回退滑窗（%s）: %s", rel_path, e)
    return _chunk_sliding(source, rel_path)


def _chunk_python(source: str, rel_path: str) -> list[CodeChunk]:
    """Python 文件用 AST 分块：按函数/类切。

    只遍历顶层节点和类内的方法，避免嵌套函数被重复切分。
    例如 class Foo 的 bar 方法只会作为 Foo.bar 被索引一次，
    不会同时作为 Foo 整体和 bar 单独被索引两次。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # 语法错误（如未写完的代码）→ 滑窗兜底
        return _chunk_sliding(source, rel_path)

    chunks: list[CodeChunk] = []
    lines = source.splitlines(keepends=True)

    def _add_chunk(node: ast.AST, type_name: str, name: str) -> None:
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        code = "".join(lines[start - 1 : end])

        if end - start + 1 > MAX_CHUNK_LINES:
            sub_chunks = _sliding_sub_chunks(
                code, start, end, rel_path, type_name=type_name,
                name=name, lines_cache=code.splitlines(keepends=True),
            )
            chunks.extend(sub_chunks)
        else:
            chunks.append(CodeChunk(
                file=rel_path,
                type=type_name,
                name=name,
                start_line=start,
                end_line=end,
                code=code,
                content_hash=hashlib.md5(code.encode("utf-8")).hexdigest(),
            ))

    # 只遍历顶层节点（tree.body），不使用 ast.walk 避免重复
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _add_chunk(node, type(node).__name__, node.name)
        elif isinstance(node, ast.ClassDef):
            # 类节点：提取其中的方法，而非整个类体
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_name = f"{node.name}.{child.name}"
                    _add_chunk(child, type(child).__name__, method_name)

    # 没有任何函数/类的文件（如纯配置脚本）→ 整体滑窗
    if not chunks:
        return _chunk_sliding(source, rel_path)

    return chunks


def _chunk_sliding(source: str, rel_path: str) -> list[CodeChunk]:
    """滑窗分块：按行切，带 overlap。用于非 Python 文件和语法错误的 Python。"""
    lines = source.splitlines(keepends=True)
    return _sliding_sub_chunks(
        source, start=1, end=len(lines) + 1,
        rel_path=rel_path, type_name="block", name="",
        lines_cache=lines,
    )


def _sliding_sub_chunks(
    code: str,
    start: int,
    end: int,
    rel_path: str,
    type_name: str,
    name: str,
    lines_cache: list[str] | None = None,
) -> list[CodeChunk]:
    """对一段代码做滑窗切分。

    每个窗口 MAX_CHUNK_LINES 行，步长 = MAX_CHUNK_LINES - SLIDING_OVERLAP，
    保证相邻窗口有 overlap，边界语义不丢。
    """
    lines = lines_cache if lines_cache is not None else code.splitlines(keepends=True)
    total = len(lines)
    if total == 0:
        return []

    chunks: list[CodeChunk] = []
    step = max(1, MAX_CHUNK_LINES - SLIDING_OVERLAP)
    window_start = 0
    while window_start < total:
        window_end = min(window_start + MAX_CHUNK_LINES, total)
        window_code = "".join(lines[window_start:window_end])
        abs_start = start + window_start
        abs_end = start + window_end - 1

        chunk_name = name if name else f"L{abs_start}-L{abs_end}"
        # 多窗口时加后缀区分
        if window_start > 0:
            chunk_name = f"{name}_part{window_start // step + 1}" if name else chunk_name

        chunks.append(CodeChunk(
            file=rel_path,
            type=type_name,
            name=chunk_name,
            start_line=abs_start,
            end_line=abs_end,
            code=window_code,
            content_hash=hashlib.md5(window_code.encode("utf-8")).hexdigest(),
        ))

        if window_end >= total:
            break
        window_start += step

    return chunks


def walk_indexable_files(project_root: Path) -> list[Path]:
    """遍历项目，返回可索引的文件列表（跳过 SKIP_DIRS、只收白名单扩展名）。"""
    files: list[Path] = []
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        # 跳过 .codebot 自身（避免索引会话/日志/向量库）
        if ".codebot" in path.parts:
            continue
        # 跳过 SKIP_DIRS
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in INDEXABLE_EXTENSIONS:
            files.append(path)
    return files
