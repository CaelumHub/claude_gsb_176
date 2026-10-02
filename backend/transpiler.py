# -*- coding: utf-8 -*-
"""
MiniLang -> Python 转译器（源码到源码的代码生成）。

复用现有编译前端的全部结果，而不另造一套前端：
  * 词法 / 语法结果（``lexer`` / ``parser`` 产出的 AST）决定程序结构；
  * 语义分析结果（符号绑定、作用域归属、静态类型）决定每个标识符如何落地。

与字节码生成器（codegen.py）是 AST 的两个并列消费者：codegen 面向栈式 VM，
本模块面向"可直接在其它 Python 环境运行"的等价脚本。

语义保真要点（与 vm.py 的运行时行为逐条对齐）：
  * 作用域：语义阶段的块作用域比 Python 函数作用域更细，转译时给每个
    变量/形参符号分配全局唯一的名字，遮蔽、块内声明、for 头变量都能正确表达；
  * 函数：函数声明提升（先输出全部 def），支持互相调用与递归；
  * 全局变量：函数内改写顶层变量时自动补 ``global`` 声明；
  * 真值：MiniLang 仅 ``null``/``false`` 为假（0、""、[] 都为真），统一走 ml_truthy；
  * 逻辑运算：&& / || 短路且返回操作数本身（用立即调用的 lambda 只求值一次）；
  * 运算：+ 的数字/字符串/列表多态、整数 / 得浮点、列表身份相等、
    bool 不参与数值运算等差异，全部由 ml_* 运行时小函数收口；
  * 循环：C 风格 for 翻译成标志位 + 双层 while，保证 break/continue 语义一致；
  * 内置函数：print/range 含端点/type 的 int 归类/round 单参保留一位等，
    逐个按 VM 语义实现。

只有在词法/语法/语义**全部无错误**时才产出目标代码；否则返回诊断且不给出
错误的 Python 代码。
"""

import json

from . import compiler as compiler_mod
from . import ast_nodes as ast
from . import symbols as sym


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def transpile_source(source: str) -> dict:
    """把一段 MiniLang 源码转译为等价 Python。

    返回结构：
        ok           —— 是否成功（无任何 error 级诊断）
        python       —— 生成的 Python 源码（失败时为空串）
        diagnostics  —— 编译前端的完整诊断（含警告），供页面渲染
        stage        —— 编译流水线停在哪个阶段
        error_count / warning_count
    """
    result = compiler_mod.compile_source(source)
    diagnostics = result.diagnostics.to_list()
    error_count = len(result.diagnostics.errors())
    warning_count = len(result.diagnostics.warnings())

    if not result.success or error_count:
        return {
            "ok": False,
            "python": "",
            "diagnostics": diagnostics,
            "stage": result.stage,
            "error_count": error_count,
            "warning_count": warning_count,
        }

    emitter = _PyEmitter(result.ast, result.symbol_table)
    code = emitter.generate()

    # 兜底自检：生成物应当是合法 Python（正常情况下恒成立）。
    try:
        compile(code, "<transpiled>", "exec")
    except SyntaxError as e:
        return {
            "ok": False,
            "python": "",
            "diagnostics": [{
                "severity": "error", "phase": "semantic", "kind": "runtime",
                "message": f"转译产物自检失败（生成的 Python 不合法）：{e}",
                "line": 1, "column": 1, "length": 1, "fix": "这是转译器内部缺陷，请检查源码中是否有极端写法。",
                "source_line": "", "related": [],
            }],
            "stage": "transpiled",
            "error_count": 1,
            "warning_count": warning_count,
        }

    return {
        "ok": True,
        "python": code,
        "diagnostics": diagnostics,
        "stage": "transpiled",
        "error_count": 0,
        "warning_count": warning_count,
    }


# ---------------------------------------------------------------------------
# 运行时前缀（按实际用到的特性裁剪）
# ---------------------------------------------------------------------------
_HEADER = '''\
# -*- coding: utf-8 -*-
# 本文件由 MiniLang 平台自动转译生成，语义与对应 MiniLang 程序等价。
# 顶部以 ml_ 开头的小函数是 MiniLang 运行时语义的支撑代码，请勿手改。
'''

# 每个特性片段：依赖的 import 与函数定义。键由发射器在遍历时置位。
_IMPORT_MATH = "import math as _ml_math"
_IMPORT_TIME = "import time as _ml_time"
_IMPORT_RANDOM = "import random as _ml_random"


def _section_truth():
    return "def ml_truthy(v):\n" \
           "    # MiniLang：仅 None(null) 与 False 为假，0 / \"\" / [] 均为真\n" \
           "    return v is not None and v is not False\n"


def _section_eq():
    return "def ml_eq(a, b):\n" \
           "    # 列表按身份比较；None、bool/int 混用的规则与 MiniLang 一致\n" \
           "    if isinstance(a, list) and isinstance(b, list):\n" \
           "        return a is b\n" \
           "    if a is None or b is None:\n" \
           "        return a is b\n" \
           "    if isinstance(a, bool) != isinstance(b, bool):\n" \
           "        return False\n" \
           "    return a == b\n"


def _section_str():
    return "def ml_to_str(v):\n" \
           "    if v is None:\n" \
           "        return \"null\"\n" \
           "    if isinstance(v, bool):\n" \
           "        return \"true\" if v else \"false\"\n" \
           "    if isinstance(v, float):\n" \
           "        return repr(v)\n" \
           "    return str(v)\n"


def _section_print():
    return "def ml_to_display(v):\n" \
           "    if isinstance(v, list):\n" \
           "        return \"[\" + \", \".join(ml_to_display(x) for x in v) + \"]\"\n" \
           "    if v is None:\n" \
           "        return \"null\"\n" \
           "    if isinstance(v, bool):\n" \
           "        return \"true\" if v else \"false\"\n" \
           "    if isinstance(v, float):\n" \
           "        if v == int(v):\n" \
           "            return f\"{v:.1f}\"\n" \
           "        return repr(v)\n" \
           "    return str(v)\n" \
           "\n" \
           "def ml_print(*args):\n" \
           "    print(\" \".join(ml_to_display(a) for a in args))\n"


def _section_add():
    return "def ml_add(a, b):\n" \
           "    # 数字相加（bool 视作 0/1）；任一侧为字符串则拼接；列表拼接\n" \
           "    if isinstance(a, (int, float)) and isinstance(b, (int, float)):\n" \
           "        return a + b\n" \
           "    if isinstance(a, str) or isinstance(b, str):\n" \
           "        return ml_to_str(a) + ml_to_str(b)\n" \
           "    if isinstance(a, list) and isinstance(b, list):\n" \
           "        return a + b\n" \
           "    raise TypeError(f\"ml_add: 不支持 {type(a).__name__} 与 {type(b).__name__} 相加\")\n"


def _num_pair_body():
    return "    for v in (a, b):\n" \
           "        if isinstance(v, bool) or not isinstance(v, (int, float)):\n" \
           "            raise TypeError(f\"ml_ 数值运算要求数字，实际得到 {type(v).__name__}\")\n"


def _section_sub():
    return "def ml_sub(a, b):\n" + _num_pair_body() + "    return a - b\n"


def _section_mul():
    return "def ml_mul(a, b):\n" \
           "    if isinstance(a, (int, float)) and isinstance(b, (int, float)):\n" \
           "        return a * b\n" \
           "    if isinstance(a, str) and isinstance(b, int):\n" \
           "        return a * b\n" \
           "    raise TypeError(\"ml_mul: 只能是数字相乘或字符串乘整数\")\n"


def _section_div():
    return "def ml_div(a, b):\n" + _num_pair_body() + \
           "    if b == 0:\n" \
           "        raise ZeroDivisionError(\"除以零\")\n" \
           "    return a / b\n"


def _section_mod():
    return "def ml_mod(a, b):\n" + _num_pair_body() + \
           "    if b == 0:\n" \
           "        raise ZeroDivisionError(\"除以零\")\n" \
           "    return a % b\n"


def _section_neg():
    return "def ml_neg(v):\n" \
           "    if isinstance(v, bool) or not isinstance(v, (int, float)):\n" \
           "        raise TypeError(f\"ml_neg: 要求数字，实际得到 {type(v).__name__}\")\n" \
           "    return -v\n"


def _section_cmp(name, op):
    return f"def ml_{name}(a, b):\n" \
           "    if isinstance(a, list) or isinstance(b, list):\n" \
           "        raise TypeError(\"列表不能使用关系比较\")\n" \
           f"    return a {op} b\n"


def _section_push():
    return "def ml_push(lst, v):\n" \
           "    if not isinstance(lst, list):\n" \
           "        raise TypeError(\"push 的第一个参数必须是列表\")\n" \
           "    lst.append(v)\n" \
           "    return lst\n" \
           "\n" \
           "def ml_pop(lst):\n" \
           "    if not isinstance(lst, list):\n" \
           "        raise TypeError(\"pop 的参数必须是列表\")\n" \
           "    if not lst:\n" \
           "        return None\n" \
           "    return lst.pop()\n"


def _section_type():
    return "def ml_type(v):\n" \
           "    if v is None:\n" \
           "        return \"null\"\n" \
           "    if isinstance(v, int):\n" \
           "        return \"int\"   # bool 是 int 子类，MiniLang 同样归类为 int\n" \
           "    if isinstance(v, float):\n" \
           "        return \"float\"\n" \
           "    if isinstance(v, str):\n" \
           "        return \"string\"\n" \
           "    if isinstance(v, list):\n" \
           "        return \"list\"\n" \
           "    if callable(v):\n" \
           "        return \"function\"\n" \
           "    return type(v).__name__\n"


def _section_int():
    return "def ml_int(v):\n" \
           "    if isinstance(v, bool):\n" \
           "        return 1 if v else 0\n" \
           "    if isinstance(v, int):\n" \
           "        return v\n" \
           "    if isinstance(v, float):\n" \
           "        return int(v)\n" \
           "    if isinstance(v, str):\n" \
           "        try:\n" \
           "            return int(v.strip())\n" \
           "        except ValueError:\n" \
           "            return 0\n" \
           "    return 0\n"


def _section_float():
    return "def ml_float(v):\n" \
           "    if isinstance(v, (int, float)):\n" \
           "        return float(v)\n" \
           "    if isinstance(v, str):\n" \
           "        try:\n" \
           "            return float(v.strip())\n" \
           "        except ValueError:\n" \
           "            return 0.0\n" \
           "    return 0.0\n"


def _section_range():
    return "def ml_range(*args):\n" \
           "    # MiniLang 的 range 上界包含端点\n" \
           "    if len(args) == 1:\n" \
           "        start, stop, step = 0, args[0], 1\n" \
           "    elif len(args) == 2:\n" \
           "        start, stop = args[0], args[1]\n" \
           "        step = 1\n" \
           "    else:\n" \
           "        start, stop, step = args[0], args[1], args[2]\n" \
           "    return list(range(start, stop + 1, step))\n"


def _section_floor_ceil():
    return "def ml_floor(v):\n" \
           "    return int(_ml_math.floor(v))\n" \
           "\n" \
           "def ml_ceil(v):\n" \
           "    return int(_ml_math.ceil(v))\n"


def _section_round():
    return "def ml_round(*args):\n" \
           "    # 单个参数时 MiniLang 保留一位小数\n" \
           "    if len(args) == 2:\n" \
           "        return round(args[0], args[1])\n" \
           "    return round(args[0], 1)\n"


def _section_input():
    return "def ml_input():\n" \
           "    try:\n" \
           "        return input()\n" \
           "    except EOFError:\n" \
           "        return \"\"\n"


def _section_exit():
    return "def ml_exit(code=0):\n" \
           "    raise SystemExit(code)\n"


# 特性 -> 需要额外引入的 import
_FEATURE_IMPORTS = {
    "sqrt": [_IMPORT_MATH],
    "floor": [_IMPORT_MATH],
    "ceil": [_IMPORT_MATH],
    "time": [_IMPORT_TIME],
    "random": [_IMPORT_RANDOM],
}


# ---------------------------------------------------------------------------
# 发射器
# ---------------------------------------------------------------------------
class _PyEmitter:
    """遍历语义分析后的 AST，逐行产出 Python 文本。"""

    # 二元运算符 -> (Python 表达式模板占位由代码处理)
    _BINOP = {
        "+": "ml_add", "-": "ml_sub", "*": "ml_mul",
        "/": "ml_div", "%": "ml_mod", "==": "ml_eq",
        "<": "ml_lt", "<=": "ml_le", ">": "ml_gt", ">=": "ml_ge",
    }

    # 内置函数名 -> 生成代码里的可调用表达式（None 表示沿用 Python 同名内置）
    _BUILTIN_CALL = {
        "print": "ml_print",
        "len": None,
        "push": "ml_push",
        "pop": "ml_pop",
        "type": "ml_type",
        "str": "ml_to_str",
        "int": "ml_int",
        "float": "ml_float",
        "range": "ml_range",
        "abs": None,
        "min": None,
        "max": None,
        "sqrt": "_ml_math.sqrt",
        "floor": "ml_floor",
        "ceil": "ml_ceil",
        "round": "ml_round",
        "input": "ml_input",
        "time": "_ml_time.time",
        "random": "_ml_random.random",
        "exit": "ml_exit",
    }

    def __init__(self, program: ast.Program, symbol_table):
        self.program = program
        self.symbol_table = symbol_table
        self.lines = []          # (indent, text)
        self.indent = 0
        self.needs = set()       # 用到的运行时特性
        self.loop_stack = []     # [{"kind": "while"|"for", "flag": 名字}]
        self._uid = 0
        self._var_names = {}     # id(symbol) -> python 名字
        self._collect_var_names()

    # ------------------------------------------------------------------
    # 命名
    # ------------------------------------------------------------------
    def _fresh(self, prefix):
        self._uid += 1
        return f"_{prefix}_{self._uid}"

    def _collect_var_names(self):
        """为每个变量/形参符号分配全局唯一的 Python 名。"""
        counter = 0
        for scope in self.symbol_table.scopes:
            for s in scope.symbols.values():
                if s.kind in (sym.KIND_VARIABLE, sym.KIND_PARAMETER):
                    counter += 1
                    base = "".join(ch for ch in s.name if ch.isalnum() or ch == "_")
                    if not base or base[0].isdigit():
                        base = "v" + base
                    self._var_names[id(s)] = f"_mlv_{counter}_{base}"

    def _name_of(self, node):
        """标识符节点落地成的 Python 名（依据语义阶段绑定的符号）。"""
        s = getattr(node, "symbol", None)
        if s is not None:
            if s.kind in (sym.KIND_VARIABLE, sym.KIND_PARAMETER):
                return self._var_names.get(id(s), node.name)
            if s.kind == sym.KIND_FUNCTION:
                return s.name          # 函数名原样保留（def 提升到顶部）
            if s.kind == sym.KIND_BUILTIN:
                target = self._BUILTIN_CALL.get(s.name)
                return target if target is not None else s.name
        return node.name

    # ------------------------------------------------------------------
    # 总入口
    # ------------------------------------------------------------------
    def generate(self):
        funcs = [d for d in self.program.declarations if isinstance(d, ast.FunctionDecl)]
        stmts = [d for d in self.program.declarations if isinstance(d, ast.Stmt)]

        # 先生成函数（def 提升到顶层）与顶层语句，同时收集 needs 特性
        for fn in funcs:
            self._function(fn)
        for s in stmts:
            self._stmt(s)

        prelude = self._build_prelude()
        parts = [_HEADER.rstrip("\n"), prelude]
        parts.extend(self._render_lines(self.lines))
        return "\n".join(p for p in parts if p is not None) + "\n"

    def _build_prelude(self):
        imports = []
        for feat, imps in _FEATURE_IMPORTS.items():
            if feat in self.needs:
                for imp in imps:
                    if imp not in imports:
                        imports.append(imp)

        sections = []
        if "truth" in self.needs:
            sections.append(_section_truth())
        if "eq" in self.needs:
            sections.append(_section_eq())
        if "add" in self.needs:
            sections.append(_section_str())
            sections.append(_section_add())
        elif "str" in self.needs:
            sections.append(_section_str())
        if "print" in self.needs:
            sections.append(_section_print())
        if "sub" in self.needs:
            sections.append(_section_sub())
        if "mul" in self.needs:
            sections.append(_section_mul())
        if "div" in self.needs:
            sections.append(_section_div())
        if "mod" in self.needs:
            sections.append(_section_mod())
        if "neg" in self.needs:
            sections.append(_section_neg())
        cmp_map = [("lt", "<"), ("le", "<="), ("gt", ">"), ("ge", ">=")]
        for key, op in cmp_map:
            if key in self.needs:
                sections.append(_section_cmp(key, op))
        if "push" in self.needs or "pop" in self.needs:
            sections.append(_section_push())
        if "type" in self.needs:
            sections.append(_section_type())
        if "int" in self.needs:
            sections.append(_section_int())
        if "float" in self.needs:
            sections.append(_section_float())
        if "range" in self.needs:
            sections.append(_section_range())
        if "floor" in self.needs or "ceil" in self.needs:
            sections.append(_section_floor_ceil())
        if "round" in self.needs:
            sections.append(_section_round())
        if "input" in self.needs:
            sections.append(_section_input())
        if "exit" in self.needs:
            sections.append(_section_exit())

        blocks = []
        if imports:
            blocks.append("\n".join(imports))
        if sections:
            blocks.append("\n".join(sections))
        return "\n\n".join(blocks)

    def _render_lines(self, lines):
        out = []
        for indent, text in lines:
            if text == "":
                out.append("")
            else:
                out.append("    " * indent + text)
        return out

    # ------------------------------------------------------------------
    # 行发射原语
    # ------------------------------------------------------------------
    def _emit(self, text):
        self.lines.append((self.indent, text))

    def _emit_blank(self):
        self.lines.append((0, ""))

    def _suite(self, stmts, indent):
        """在给定缩进上发射一个语句块（空块补 pass）。"""
        old = self.indent
        self.indent = indent
        if not stmts:
            self._emit("pass")
        else:
            for s in stmts:
                self._stmt(s)
        self.indent = old

    # ------------------------------------------------------------------
    # 函数
    # ------------------------------------------------------------------
    def _function_scope(self, fn):
        for scope in self.symbol_table.scopes:
            if scope.scope_type == sym.SCOPE_FUNCTION and scope.name == fn.name:
                return scope
        return None

    def _global_assignments(self, block):
        """收集函数体内被赋值、且绑定到全局作用域的变量名（需要 global 声明）。"""
        found = []

        def note(target):
            s = getattr(target, "symbol", None)
            if s is not None and s.kind == sym.KIND_VARIABLE \
                    and s.scope.scope_type == sym.SCOPE_GLOBAL:
                py = self._var_names.get(id(s))
                if py and py not in found:
                    found.append(py)

        def walk(stmt):
            if stmt is None:
                return
            if isinstance(stmt, ast.AssignStmt):
                if isinstance(stmt.target, ast.Identifier):
                    note(stmt.target)
            elif isinstance(stmt, ast.ExprStmt):
                walk(stmt.expr)
            elif isinstance(stmt, ast.Block):
                for x in stmt.statements:
                    walk(x)
            elif isinstance(stmt, ast.IfStmt):
                for _, body in stmt.branches:
                    for x in body.statements:
                        walk(x)
                if stmt.else_block:
                    for x in stmt.else_block.statements:
                        walk(x)
            elif isinstance(stmt, ast.WhileStmt):
                for x in stmt.body.statements:
                    walk(x)
            elif isinstance(stmt, ast.ForStmt):
                if stmt.init is not None:
                    walk(stmt.init)
                if stmt.increment is not None:
                    walk(stmt.increment)
                for x in stmt.body.statements:
                    walk(x)

        for s in block.statements:
            walk(s)
        return found

    def _function(self, fn: ast.FunctionDecl):
        scope = self._function_scope(fn)
        params = []
        if scope is not None:
            ordered = [scope.symbols[p] for p in fn.params if p in scope.symbols]
            params = [self._var_names.get(id(s), s.name) for s in ordered]
        else:
            params = list(fn.params)

        self._emit_blank()
        self._emit(f"def {fn.name}({', '.join(params)}):")
        body_indent = self.indent + 1
        old = self.indent
        self.indent = body_indent

        globals_ = self._global_assignments(fn.body)
        if globals_:
            self._emit("global " + ", ".join(globals_))
        if not fn.body.statements and not globals_:
            self._emit("pass")
        else:
            for s in fn.body.statements:
                self._stmt(s)
        self.indent = old

    # ------------------------------------------------------------------
    # 语句
    # ------------------------------------------------------------------
    def _stmt(self, s):
        if s is None:
            return
        if isinstance(s, ast.Block):
            # Python 没有块作用域；块内变量已被唯一重命名，直接摊平即可
            for x in s.statements:
                self._stmt(x)
        elif isinstance(s, ast.VarDecl):
            self._var_decl(s)
        elif isinstance(s, ast.AssignStmt):
            self._assign(s)
        elif isinstance(s, ast.ExprStmt):
            if isinstance(s.expr, ast.AssignStmt):
                self._assign(s.expr)
            else:
                self._emit(self._expr(s.expr))
        elif isinstance(s, ast.PrintStmt):
            self.needs.add("print")
            args = ", ".join(self._expr(a) for a in s.args)
            self._emit(f"ml_print({args})")
        elif isinstance(s, ast.IfStmt):
            self._if(s)
        elif isinstance(s, ast.WhileStmt):
            self._while(s)
        elif isinstance(s, ast.ForStmt):
            self._for(s)
        elif isinstance(s, ast.ReturnStmt):
            if s.value is not None:
                self._emit("return " + self._expr(s.value))
            else:
                self._emit("return")
        elif isinstance(s, ast.BreakStmt):
            self._break()
        elif isinstance(s, ast.ContinueStmt):
            self._continue()
        elif isinstance(s, ast.FunctionDecl):
            self._function(s)

    def _var_decl(self, s: ast.VarDecl):
        name = self._var_names.get(id(s.symbol), s.name) if s.symbol else s.name
        if s.initializer is not None:
            self._emit(f"{name} = {self._expr(s.initializer)}")
        else:
            self._emit(f"{name} = None")

    def _target_name(self, target):
        s = getattr(target, "symbol", None)
        if s is not None and s.kind in (sym.KIND_VARIABLE, sym.KIND_PARAMETER):
            return self._var_names.get(id(s), target.name)
        return target.name

    def _assign(self, s: ast.AssignStmt):
        if isinstance(s.target, ast.Identifier):
            name = self._target_name(s.target)
            if s.op == "=":
                self._emit(f"{name} = {self._expr(s.value)}")
            else:
                helper = self._binop_helper(s.op[0])
                self._emit(f"{name} = {helper}({name}, {self._expr(s.value)})")
            return
        if isinstance(s.target, ast.IndexExpr):
            tgt = self._fresh("t")
            idx = self._fresh("i")
            self._emit(f"{tgt} = {self._expr(s.target.target)}")
            self._emit(f"{idx} = {self._expr(s.target.index)}")
            if s.op == "=":
                self._emit(f"{tgt}[{idx}] = {self._expr(s.value)}")
            else:
                helper = self._binop_helper(s.op[0])
                self._emit(f"{tgt}[{idx}] = {helper}({tgt}[{idx}], {self._expr(s.value)})")

    def _if(self, s: ast.IfStmt):
        first = True
        for cond, body in s.branches:
            kw = "if" if first else "elif"
            self._emit(f"{kw} {self._truth(cond)}:")
            self._suite(body.statements, self.indent + 1)
            first = False
        if s.else_block is not None:
            self._emit("else:")
            self._suite(s.else_block.statements, self.indent + 1)

    def _while(self, s: ast.WhileStmt):
        self._emit(f"while {self._truth(s.condition)}:")
        self.loop_stack.append({"kind": "while"})
        self._suite(s.body.statements, self.indent + 1)
        self.loop_stack.pop()

    def _for(self, s: ast.ForStmt):
        # C 风格 for 的结构化翻译（break/continue 与 MiniLang 完全一致）：
        #   <init>
        #   <flag> = False
        #   while <cond>:
        #       while not <flag>:
        #           <body>            # continue -> break（跳出内层，落到增量）
        #           break             # 正常跑完一轮也跳出内层
        #       if <flag>: break      # for 的 break -> 真正退出外层
        #       <increment>
        flag = self._fresh("brk")
        if s.init is not None:
            self._stmt(s.init)
        self._emit(f"{flag} = False")
        cond = self._truth(s.condition) if s.condition is not None else "True"
        self._emit(f"while {cond}:")
        outer = self.indent + 1
        old = self.indent
        self.indent = outer
        self._emit(f"while not {flag}:")
        inner = self.indent + 1
        self.indent = inner
        self.loop_stack.append({"kind": "for", "flag": flag})
        if s.body.statements:
            for x in s.body.statements:
                self._stmt(x)
        else:
            self._emit("pass")
        self.loop_stack.pop()
        self._emit("break")  # 完整执行完一轮：跳出内层，进入增量
        self.indent = outer
        self._emit(f"if {flag}:")
        self._emit("    break")
        if s.increment is not None:
            inc = s.increment
            if isinstance(inc, ast.AssignStmt):
                self._assign(inc)
            else:
                self._emit(self._expr(inc))
        self.indent = old

    def _break(self):
        loop = self.loop_stack[-1] if self.loop_stack else None
        if loop is not None and loop["kind"] == "for":
            # for 的 break：置标志并跳出内层；外层头部检测后真正退出
            self._emit(f"{loop['flag']} = True")
        self._emit("break")

    def _continue(self):
        loop = self.loop_stack[-1] if self.loop_stack else None
        if loop is not None and loop["kind"] == "for":
            # for 的 continue：跳出内层单次循环，正好落到增量表达式
            self._emit("break")
        else:
            self._emit("continue")

    # ------------------------------------------------------------------
    # 表达式
    # ------------------------------------------------------------------
    def _truth(self, e):
        self.needs.add("truth")
        return f"ml_truthy({self._expr(e)})"

    def _binop_helper(self, op):
        helper = self._BINOP[op]
        if op == "!=":
            return "ml_eq"
        if op == "==":
            self.needs.add("eq")
        elif op in ("<", "<="):
            self.needs.add("lt" if op == "<" else "le")
        elif op in (">", ">="):
            self.needs.add("gt" if op == ">" else "ge")
        elif helper.startswith("ml_"):
            self.needs.add(helper[3:])
        return helper

    def _expr(self, e) -> str:
        if isinstance(e, ast.NumberLiteral):
            return repr(e.value)
        if isinstance(e, ast.StringLiteral):
            return json.dumps(e.value, ensure_ascii=False)
        if isinstance(e, ast.BoolLiteral):
            return "True" if e.value else "False"
        if isinstance(e, ast.NullLiteral):
            return "None"
        if isinstance(e, ast.Identifier):
            return self._name_of(e)
        if isinstance(e, ast.UnaryExpr):
            return self._unary(e)
        if isinstance(e, ast.BinaryExpr):
            return self._binary(e)
        if isinstance(e, ast.LogicalExpr):
            return self._logical(e)
        if isinstance(e, ast.CallExpr):
            return self._call(e)
        if isinstance(e, ast.IndexExpr):
            return f"{self._expr(e.target)}[{self._expr(e.index)}]"
        if isinstance(e, ast.ListLiteral):
            return "[" + ", ".join(self._expr(x) for x in e.elements) + "]"
        return "None"

    def _unary(self, e: ast.UnaryExpr):
        operand = self._expr(e.operand)
        if e.op == "-":
            self.needs.add("neg")
            return f"(ml_neg({operand}))"
        # !
        self.needs.add("truth")
        return f"((not ml_truthy({operand})))"

    def _binary(self, e: ast.BinaryExpr):
        left = self._expr(e.left)
        right = self._expr(e.right)
        if e.op == "!=":
            self.needs.add("eq")
            return f"((not ml_eq({left}, {right})))"
        helper = self._binop_helper(e.op)
        return f"({helper}({left}, {right}))"

    def _logical(self, e: ast.LogicalExpr):
        # 用立即调用的 lambda 保证左操作数只求值一次，同时维持短路与"返回操作数本身"
        var = self._fresh("v")
        left = self._expr(e.left)
        right = self._expr(e.right)
        self.needs.add("truth")
        if e.op == "||":
            return f"((lambda {var}: {var} if ml_truthy({var}) else ({right}))({left}))"
        return f"((lambda {var}: ({right}) if ml_truthy({var}) else {var})({left}))"

    def _builtin_callee(self, name):
        target = self._BUILTIN_CALL.get(name)
        self.needs.add(name)
        return target if target is not None else name

    def _call(self, e: ast.CallExpr):
        if isinstance(e.callee, ast.Identifier):
            s = getattr(e.callee, "symbol", None)
            if s is not None and s.kind == sym.KIND_BUILTIN:
                callee = self._builtin_callee(s.name)
            else:
                callee = self._name_of(e.callee)
        else:
            callee = self._expr(e.callee)
        args = ", ".join(self._expr(a) for a in e.args)
        return f"{callee}({args})"
