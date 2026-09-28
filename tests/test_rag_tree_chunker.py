"""多语言 tree-sitter 语义分块测试。

依赖 tree-sitter（optional）：未安装时整组跳过，不影响 CI / 无 RAG 环境。
覆盖：js/ts/go/java/rust/c/cpp/ruby 的函数/类/方法切分语义、嵌套函数去重、
超长块二次切分、语法错误与无声明文件的滑窗回退。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter")

from codebot.rag.chunker import MAX_CHUNK_LINES, chunk_file
from codebot.rag.tree_chunker import can_handle, try_chunk


def _chunks(src: str, suffix: str, name: str = "mod"):
    """便捷封装：确保返回 list（None 会被标记为断言失败点）。"""
    result = try_chunk(src, suffix, name + suffix)
    assert result is not None, f"{suffix} 语义分块返回 None（回退滑窗了），需人工确认是否预期"
    return result


def _names(src: str, suffix: str) -> set[str]:
    return {c.name for c in _chunks(src, suffix)}


class TestJavaScript:
    def test_top_functions_and_class_methods(self):
        src = """\
export function topFn() { return 1; }
const arrow = (x) => x * 2;
var oldFn = function (y) { return y; };
export default class Foo {
  bar() { const inner = () => 1; }
  static baz(a, b) {}
}
"""
        names = _names(src, ".js")
        # 顶层函数与 arrow/function 赋值都是独立函数块
        assert {"topFn", "arrow", "oldFn"} <= names
        # 类只拆方法，带类名前缀
        assert {"Foo.bar", "Foo.baz"} <= names
        # 方法体内嵌套的闭包 inner 不应单独成块
        assert not any("inner" == n or "inner" in n for n in names)
        assert not any(n == "Foo" for n in names)  # 类自身不建块

    def test_function_line_span(self):
        src = """\
export function span() {
  return 42;
}
"""
        chunks = _chunks(src, ".js")
        c = next(x for x in chunks if x.name == "span")
        assert c.start_line == 1 and c.end_line == 3
        assert "return 42" in c.code


class TestTypeScript:
    def test_interface_enum_typealias_as_whole_units(self):
        src = """\
export interface User { id: number }
type ID = string;
enum Color { Red }
export function greet(): string { return "hi"; }
"""
        chunks = _chunks(src, ".ts")
        kinds = {c.name: c.type for c in chunks}
        assert kinds["User"] == "type"
        assert kinds["ID"] == "type"
        assert kinds["Color"] == "type"
        assert kinds["greet"] == "function"

    def test_export_arrow_and_class(self):
        src = """\
export const run = (n: number) => n + 1;
export class Store {
  add(n: number) { return n; }
}
"""
        names = _names(src, ".ts")
        assert "run" in names and "Store.add" in names


class TestGo:
    def test_func_method_and_type_spec(self):
        src = """\
package main
type Point struct { X, Y int }
type Shape interface { Area() float64 }
type (
    A struct{ X int }
    B struct{ Y int }
)
func (p Point) Dist() int { return p.X }
func Add(a, b int) int { return a + b }
"""
        chunks = _chunks(src, ".go")
        kinds = {c.name: c.type for c in chunks}
        assert kinds["Point"] == "type"
        assert kinds["Shape"] == "type"
        # 分组 type(...) 里每个 type_spec 独立成块
        assert kinds["A"] == "type" and kinds["B"] == "type"
        assert kinds["Dist"] == "function"
        assert kinds["Add"] == "function"


class TestJava:
    def test_methods_ctor_inner_class(self):
        src = """\
public class Calc {
    public Calc() {}
    public int add(int a) { return a; }
    static int sub(int a, int b) { return a - b; }
    class Inner { void go() {} }
}
"""
        chunks = _chunks(src, ".java")
        kinds = {c.name: c.type for c in chunks}
        assert kinds["Calc.Calc"] == "method"  # 构造函数
        assert kinds["Calc.add"] == "method"
        assert kinds["Calc.sub"] == "method"
        # 内部类整块（不递归拆方法），带外层类前缀
        assert kinds["Calc.Inner"] == "type"


class TestRust:
    def test_impl_trait_methods_and_items(self):
        src = """\
pub struct Point { x: f64 }
pub trait Area { fn area(&self) -> f64; }
impl Area for Point {
    fn area(&self) -> f64 { self.x }
}
impl Point {
    pub fn new() -> Self { Point { x: 0.0 } }
    fn scale(&self) {}
}
pub fn main() {}
"""
        chunks = _chunks(src, ".rs")
        kinds = {c.name: c.type for c in chunks}
        # impl 用目标类型名做前缀
        assert kinds["Point.area"] == "method"
        assert kinds["Point.new"] == "method"
        assert kinds["Point.scale"] == "method"
        # trait 里的签名方法不切碎整个 trait，整块结构体/枚举是 type
        assert kinds["Point"] == "type"
        assert kinds["main"] == "function"


class TestC:
    def test_function_and_struct_names(self):
        src = """\
struct Point { int x; int y; };
static int helper(int v) { return v * 2; }
int main(void) { return 0; }
union U { int i; };
enum Color { RED, GREEN };
"""
        chunks = _chunks(src, ".c")
        kinds = {c.name: c.type for c in chunks}
        assert kinds["Point"] == "type"
        assert kinds["U"] == "type"
        assert kinds["Color"] == "type"
        assert kinds["helper"] == "function"
        assert kinds["main"] == "function"


class TestCpp:
    def test_namespace_class_and_declarator_name(self):
        src = """\
namespace app {
class Widget {
public:
  int run(int x) { return x; }
};
struct Data { int a; };
int free_fn(double d) { return 0; }
}
int global_fn() { return 1; }
"""
        chunks = _chunks(src, ".cpp")
        kinds = {c.name: c.type for c in chunks}
        # namespace 里的类/函数按同层规则切
        assert kinds["Widget.run"] == "method"
        assert kinds["Data"] == "type"
        assert kinds["free_fn"] == "function"
        assert kinds["global_fn"] == "function"

    def test_out_of_class_definition(self):
        src = """\
class Widget {
public:
  void run(int x);
};
void Widget::run(int x) {}
"""
        chunks = _chunks(src, ".cpp")
        names = {c.name for c in chunks}
        assert any(n.endswith("Widget::run") for n in names)


class TestRuby:
    def test_class_module_methods(self):
        src = """\
class Greeter
  def initialize(name); @name = name; end
  def greet; 'hi'; end
  def self.create(name) = new(name)
end
module Helpers
  def shout(x) = x
end
def top_method(a); a + 1; end
"""
        chunks = _chunks(src, ".rb")
        kinds = {c.name: c.type for c in chunks}
        assert kinds["Greeter.initialize"] == "method"
        assert kinds["Greeter.greet"] == "method"
        assert kinds["Greeter.create"] == "method"  # 单例方法
        assert kinds["Helpers.shout"] == "method"
        assert kinds["top_method"] == "function"


class TestFallbacks:
    def test_long_function_second_pass(self):
        """超长函数被滑窗二次切分（与 python 行为一致）。"""
        body = "\n".join(f"  x{i} = {i};" for i in range(MAX_CHUNK_LINES + 20))
        src = f"export function big() {{\n{body}\n}}\n"
        chunks = _chunks(src, ".js")
        big = [c for c in chunks if c.name.startswith("big")]
        assert len(big) >= 2

    def test_syntax_error_returns_none(self):
        """语法错误 → None（chunk_file 会滑窗兜底）。"""
        assert try_chunk("function broken( {\n}\n", ".js", "b.js") is None

    def test_no_declaration_returns_none(self):
        """没有任何函数/类型声明 → None（整文件滑窗）。"""
        assert try_chunk("const a = 1;\nconsole.log(a);\n", ".js", "n.js") is None

    def test_stable_id(self, tmp_path):
        """同位置同名的函数，ID 稳定（增量 upsert 依赖）。"""
        f = tmp_path / "code.ts"
        f.write_text("export function foo() { return 1; }\n", encoding="utf-8")
        first = chunk_file(f, tmp_path)
        second = chunk_file(f, tmp_path)
        assert first and first[0].id == second[0].id

    def test_tsx_jsx_suffixes(self):
        src = "export function App() { return <div/>; }\n"
        assert "App" in _names(src, ".tsx")
        assert "App" in _names(src, ".jsx")

    def test_chunk_file_integration(self, tmp_path):
        """chunk_file 入口对 .js 走语义分块而非滑窗 block。"""
        f = tmp_path / "mod.js"
        f.write_text("export function go() { return 1; }\n", encoding="utf-8")
        chunks = chunk_file(f, tmp_path)
        assert chunks
        assert chunks[0].name == "go"
        assert chunks[0].type == "function"
        assert chunks[0].start_line == 1

    def test_can_handle(self):
        assert can_handle("a.ts")
        assert can_handle("b.rs")
        assert not can_handle("c.yaml")  # 无 grammar → False
        assert not can_handle("d.unknown")


class TestPythonUntouched:
    def test_python_still_stdlib_ast(self):
        """Python 分块仍走原有 ast 路径，语义类型不受影响。"""
        f = Path(__file__).parent / "data_py_check.py"
        # 直接用字符串写临时文件验证类型不变
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.py"
            p.write_text(
                "def foo():\n"
                "    return 1\n"
                "\n"
                "class Bar:\n"
                "    def method(self):\n"
                "        return 2\n",
                encoding="utf-8",
            )
            chunks = chunk_file(p, Path(d))
            kinds = {c.name: c.type for c in chunks}
            assert kinds["foo"] == "FunctionDef"  # 保留原有类型值
            assert kinds["Bar.method"] == "FunctionDef"
