"""多语言 AST 语义分块（tree-sitter 后端）。

chunker.py 只对 .py 用 stdlib ast 按函数/类分块，其他语言走滑窗兜底。
本模块把"语义分块"扩展到常见编译/脚本语言：

  - js / jsx / ts / tsx / go / java / rs / c / cpp / rb / bash(待扩)

原理：tree-sitter 把每种语言解析成一棵统一风格的 CST，且绝大多数语言的
声明节点都有相同的 field 约定（name field = 声明名，body field = 成员列表），
所以引擎是语言无关的——差异全部收敛在 _SPECS 配置表里：
加一门新语言 = 加几行配置，不动引擎。

分块语义与 _chunk_python 完全对齐：
  - 顶层函数/方法 → 单块；类/impl/trait 只拆其中的方法，类本身不重复建块
  - 嵌套在函数体内的闭包不再单独切（避免重复索引）
  - interface/enum/type alias/struct 这类"整块类型声明"作为一个块
  - namespace / mod / type 分组等"透明容器"里的声明按同层规则继续切
  - 超长块交给 chunker._sliding_sub_chunks 二次切分（带 overlap）
  - 语法错误 / 依赖缺失 / 未配置语言 → 返回 None，调用方回退滑窗

依赖是 optional 的（同 qdrant-client 的策略）：tree-sitter 及其 grammar 未
安装时本模块自动失效，chunker.py 行为与现在完全一致，零回归。

CodeChunk.type 语义（与 python 的 FunctionDef/ClassDef 相区分）：
  - function：顶层独立函数/方法声明
  - method：类 / impl / trait 体内的成员方法（name 带容器前缀）
  - type：整块的类、接口、枚举、结构体、类型别名等声明
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from pathlib import Path

from codebot.rag.chunker import CodeChunk, MAX_CHUNK_LINES as _MAX_CHUNK_LINES, _sliding_sub_chunks

log = logging.getLogger(__name__)

# 类型级语义标记（写入 CodeChunk.type / Qdrant payload）
_KIND_FUNCTION = "function"
_KIND_METHOD = "method"
_KIND_TYPE = "type"

# C/C++ declarator 链尾的"叶子名"节点：没有子 declarator，节点文本即声明名
_C_NAME_LEAVES = {"identifier", "field_identifier", "type_identifier",
                  "qualified_identifier", "destructor_name", "operator_name"}

# 每个后缀的提取器字段（CST 节点类型来自各 grammar 的 node-types.json）
# 提取器之间的差异是语言特性使然，无法进一步抽象，故以明文配置收敛。
_JS_DEFS = {
    "funcs": {"function_declaration", "generator_function_declaration"},
    # const f = () => {} / const f = function () {}：变量声明 + 函数值 = 独立函数块
    "value_funcs": {"arrow_function", "function_expression", "generator_function"},
    "classes": {"class_declaration"},
    "methods": {"method_definition"},
    "units": set(),
    "transparents": set(),
    "export_unwrap": True,
}
_TS_DEFS = {
    "funcs": {"function_declaration", "generator_function_declaration"},
    "value_funcs": {"arrow_function", "function_expression", "generator_function"},
    "classes": {"class_declaration", "abstract_class_declaration"},
    "methods": {"method_definition"},
    "units": {"interface_declaration", "type_alias_declaration", "enum_declaration"},
    "transparents": {"internal_module"},  # namespace NS { ... }
    "export_unwrap": True,
}
_GO_DEFS = {
    "funcs": {"function_declaration", "method_declaration"},
    "classes": set(),
    "methods": set(),
    # type Point struct { ... } 的分组容器是 type_declaration（透明），
    # 真正的定义单元是其中的 type_spec
    "units": {"type_spec"},
    "transparents": {"type_declaration"},
}
_JAVA_DEFS = {
    "funcs": set(),  # java 顶层只有类型声明，没有游离函数
    "classes": {"class_declaration", "interface_declaration",
                "enum_declaration", "record_declaration"},
    "methods": {"method_declaration", "constructor_declaration"},
    "units": set(),
    "transparents": set(),
}
_RUST_DEFS = {
    "funcs": {"function_item"},
    "classes": {"impl_item", "trait_item"},
    "methods": {"function_item", "function_signature_item"},
    "units": {"struct_item", "enum_item", "union_item", "type_item",
              "const_item", "static_item"},
    "transparents": {"mod_item"},
}
_C_DEFS = {
    "funcs": {"function_definition"},
    "classes": set(),
    "methods": set(),
    "units": {"struct_specifier", "union_specifier", "enum_specifier"},
    "transparents": set(),
    "c_family": True,  # 函数名藏在 declarator 链里，需特殊提取
}
_CPP_DEFS = {
    "funcs": {"function_definition"},
    "classes": {"class_specifier"},
    "methods": {"function_definition"},
    "units": {"struct_specifier", "union_specifier", "enum_specifier"},
    "transparents": {"namespace_definition", "template_declaration"},
    "c_family": True,
}
_RUBY_DEFS = {
    "funcs": {"method", "singleton_method"},
    "classes": {"class", "module"},
    "methods": {"method", "singleton_method"},
    "units": set(),
    "transparents": set(),
}

# suffix -> (grammar 包, 语言构造函数名, 定义配置)
# tree-sitter grammar 的 wheel 包在 PyPI 均有官方发布（tree-sitter-<lang>）。
_LANGS: dict[str, tuple[str, str, dict]] = {
    ".js": ("tree_sitter_javascript", "language", _JS_DEFS),
    ".jsx": ("tree_sitter_javascript", "language", _JS_DEFS),
    ".mjs": ("tree_sitter_javascript", "language", _JS_DEFS),
    ".cjs": ("tree_sitter_javascript", "language", _JS_DEFS),
    ".ts": ("tree_sitter_typescript", "language_typescript", _TS_DEFS),
    ".tsx": ("tree_sitter_typescript", "language_tsx", _TS_DEFS),
    ".mts": ("tree_sitter_typescript", "language_typescript", _TS_DEFS),
    ".cts": ("tree_sitter_typescript", "language_typescript", _TS_DEFS),
    ".go": ("tree_sitter_go", "language", _GO_DEFS),
    ".java": ("tree_sitter_java", "language", _JAVA_DEFS),
    ".rs": ("tree_sitter_rust", "language", _RUST_DEFS),
    ".c": ("tree_sitter_c", "language", _C_DEFS),
    ".h": ("tree_sitter_c", "language", _C_DEFS),
    ".cpp": ("tree_sitter_cpp", "language", _CPP_DEFS),
    ".hpp": ("tree_sitter_cpp", "language", _CPP_DEFS),
    ".cc": ("tree_sitter_cpp", "language", _CPP_DEFS),
    ".hxx": ("tree_sitter_cpp", "language", _CPP_DEFS),
    ".rb": ("tree_sitter_ruby", "language", _RUBY_DEFS),
}

_parser_cache: dict[str, object] = {}  # suffix -> Parser | None


class _Spec:
    """一个后缀对应的提取配置 + 解析器缓存。"""

    __slots__ = ("defs", "parser")

    def __init__(self, defs: dict) -> None:
        self.defs = defs
        self.parser = None  # 懒加载，未安装依赖时保持 None


def _get_spec(suffix: str) -> _Spec | None:
    """拿到后缀的 Spec；grammar 依赖缺失时返回 None（调用方回退滑窗）。"""
    key = suffix.lower()
    entry = _LANGS.get(key)
    if entry is None:
        return None

    parser = _parser_cache.get(key, False)
    if parser is False:  # 尚未尝试加载
        module_name, lang_fn, defs = entry
        try:
            module = importlib.import_module(module_name)
            from tree_sitter import Language, Parser

            language = Language(getattr(module, lang_fn)())
            parser = Parser(language)
        except ImportError:
            log.debug("tree-sitter grammar 未安装（%s），%s 走滑窗兜底", module_name, key)
            parser = None
        _parser_cache[key] = parser

    if parser is None:
        return None
    spec = _Spec(entry[2])
    spec.parser = parser
    return spec


def can_handle(path: str | Path) -> bool:
    """该文件扩展名是否已支持 tree-sitter 语义分块且依赖已装。"""
    return _get_spec(Path(path).suffix) is not None


def try_chunk(source: str, suffix: str, rel_path: str) -> list[CodeChunk] | None:
    """对源码做语义分块。

    返回 None 表示"不该/不能用语义分块"（语言未配置、依赖缺失、语法错误、
    文件内没有函数/类）——调用方据此回退滑窗，保证索引永不中断。
    """
    spec = _get_spec(suffix)
    if spec is None:
        return None

    try:
        tree = spec.parser.parse(source.encode("utf-8", errors="replace"))
    except Exception as e:  # pragma: no cover - 防御性兜底
        log.debug("tree-sitter 解析失败 %s: %s", rel_path, e)
        return None

    root = tree.root_node
    if root.has_error:
        # 语法错误（未写完/半截代码）→ 滑窗兜底，与 _chunk_python 一致
        return None

    defs = spec.defs
    lines = source.splitlines(keepends=True)
    chunks: list[CodeChunk] = []

    def _name_of(node, fallback: str = "") -> str:
        """提取声明名。

        多数语言：child_by_field_name('name')。
        c/c++：函数名在 declarator 链上（function_declarator -> declarator -> identifier）。
        rust impl：impl_item 没有 name，目标类型在其 'type' field。
        """
        # 1) 多数语言/类型节点：name field（class_specifier / struct_specifier / method...）
        nm = node.child_by_field_name("name")
        if nm is not None:
            return nm.text.decode("utf-8", "replace")

        # 2) c/c++ 函数定义：function_definition 没有 name field，
        #    声明名藏在 declarator 链里（function_declarator -> identifier / qualified_identifier）
        if defs.get("c_family"):
            d = node.child_by_field_name("declarator")
            while d is not None:
                if d.type in _C_NAME_LEAVES:
                    # 叶子名节点：identifier 文本即名；qualified_identifier 整段才是
                    # 完整限定名（Widget::run），不能用其 name field（只有 run）
                    return d.text.decode("utf-8", "replace")
                nm = d.child_by_field_name("name")
                if nm is not None:
                    return nm.text.decode("utf-8", "replace")
                nxt = d.child_by_field_name("declarator")
                if nxt is None:
                    break
                d = nxt
            # 类外定义 void Widget::run() {} 之类兜底
            for child in node.named_children:
                if child.type in ("field_identifier", "identifier", "qualified_identifier"):
                    return child.text.decode("utf-8", "replace")
            return fallback

        # 3) rust impl_item 没有 name field，目标类型在其 'type' field
        if node.type == "impl_item":
            t = node.child_by_field_name("type")
            if t is not None:
                return t.text.decode("utf-8", "replace")
        return fallback

    def _emit(node, prefix: str, kind: str) -> None:
        """把一个语义节点建成 CodeChunk（超长时滑窗二次切分）。"""
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        if end < start:
            return
        code = "".join(lines[start - 1 : end])

        name = prefix + _name_of(node)
        if not name:
            name = f"L{start}-L{end}"

        if end - start + 1 > _MAX_CHUNK_LINES:
            chunks.extend(_sliding_sub_chunks(
                code, start, end, rel_path,
                type_name=kind, name=name,
                lines_cache=code.splitlines(keepends=True),
            ))
        else:
            chunks.append(CodeChunk(
                file=rel_path, type=kind, name=name,
                start_line=start, end_line=end, code=code,
                content_hash=hashlib.md5(code.encode("utf-8")).hexdigest(),
            ))

    def _members(node) -> list:
        """节点的"成员/语句"层子节点（class 体、namespace 体、透明容器内层）。"""
        body = node.child_by_field_name("body")
        if body is not None:
            return list(body.named_children)
        return list(node.named_children)

    def _unwrap(stmt):
        """js/ts: export function/class/const 的声明藏在 export_statement 里。"""
        if defs.get("export_unwrap") and stmt.type == "export_statement":
            decl = stmt.child_by_field_name("declaration")
            return decl if decl is not None else stmt
        return stmt

    def _classify(node) -> str:
        """节点属于函数/类/透明容器/类型块/普通语句中的哪一类。"""
        t = node.type
        if t in defs["funcs"]:
            return "func"
        if t in defs["classes"]:
            return "class"
        if t in defs["transparents"]:
            return "transparent"
        if t in defs["units"]:
            return "unit"
        return "other"

    def _handle(node, prefix: str) -> None:
        """按 python AST 的语义处理一个"顶层/同层"声明。"""
        t = node.type
        value_funcs = defs.get("value_funcs") or ()
        if t in value_funcs:
            return  # 值节点本身不切（由其声明语句负责）

        kind = _classify(node)
        if kind == "func":
            _emit(node, prefix, _KIND_FUNCTION)
        elif kind == "unit":
            _emit(node, prefix, _KIND_TYPE)
        elif kind == "transparent":
            # namespace/mod/分组类型：其成员按同层规则继续切
            for child in _members(node):
                _handle(_unwrap(child), prefix)
        elif kind == "class":
            # 类容器：只拆其中的方法；嵌套类型整块；类自身不建块（对齐 python）
            cls_name = prefix + _name_of(node)
            for m in _members(node):
                m2 = _unwrap(m)
                if m2.type in defs["methods"]:
                    _emit(m2, cls_name + ".", _KIND_METHOD)
                elif _classify(m2) in ("class", "unit"):
                    # 内部类/枚举等：整块切（不递归拆方法，避免语义复杂化）
                    _emit(m2, cls_name + ".", _KIND_TYPE)
        # "other"（变量声明、import、表达式、注释）不切，对齐 python 语义
        # js/ts 例外：const f = () => {} / var f = function () {}
        if t in ("lexical_declaration", "variable_declaration") and defs.get("value_funcs"):
            for dec in node.named_children:
                if dec.type != "variable_declarator":
                    continue
                value = dec.child_by_field_name("value")
                if value is not None and value.type in defs["value_funcs"]:
                    _emit(dec, prefix, _KIND_FUNCTION)

    for stmt in root.named_children:
        _handle(_unwrap(stmt), "")

    if not chunks:
        return None  # 没有函数/类 → 由调用方整体滑窗（对齐 _chunk_python）
    return chunks
