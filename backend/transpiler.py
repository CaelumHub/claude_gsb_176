# -*- coding: utf-8 -*-
"""
MiniLang -> Python 转译器。

该阶段不重新实现前端：输入必须是 ``compile_source`` 已经完成词法、语法和
语义分析的 :class:`CompileResult`。生成器遍历带符号绑定的 AST，把变量、函数、
控制流和列表翻译为 Python，并在文件头放入一小段仅依赖标准库的运行时适配层，
用于保留 MiniLang 与原生 Python 不同的语义（例如除法、真值规则、列表身份比较、
``range`` 闭区间、输出格式和块级作用域）。
"""

import builtins

from . import ast_nodes as ast
from . import symbols as sym


_RUNTIME_PREAMBLE = r'''# -*- coding: utf-8 -*-
"""由 MiniLang 转译生成。顶部的 _ml_* 辅助函数用于保留 MiniLang 运行时语义。"""
import builtins
import math
import random
import sys
import time


_ML_UNDEFINED = object()


class MLList(builtins.list):
    """MiniLang 列表：可变、异构，相等比较保持引用身份语义。"""

    _next_oid = 1

    def __init__(self, values=()):
        super().__init__(values)
        self.oid = MLList._next_oid
        MLList._next_oid += 1

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return self is not other

    def __hash__(self):
        return builtins.id(self)

    def __repr__(self):
        return f"<list#{self.oid} len={builtins.len(self)}>"


class _MLBuiltin:
    def __init__(self, name, fn):
        self.name = name
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)

    def __repr__(self):
        return f"<builtin {self.name}>"


class _MLUserFunction:
    def __init__(self, name, params, fn):
        self.name = name
        self.params = params
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)

    def __repr__(self):
        return f"<function {self.name}({', '.join(self.params)})>"


def _ml_list(*values):
    return MLList(values)


def _ml_global(name):
    value = builtins.globals().get(name, _ML_UNDEFINED)
    if value is _ML_UNDEFINED:
        raise NameError(f"name {name!r} is not defined")
    return value


def _ml_value(name, value):
    if value is _ML_UNDEFINED:
        raise NameError(f"name {name!r} is not defined")
    return value


def _ml_truth(value):
    """MiniLang 真值规则：只有 null 和 false 为假。"""
    if value is None:
        return False
    if isinstance(value, builtins.bool):
        return value
    return True


def _ml_not(value):
    return not _ml_truth(value)


def _ml_is_number(value):
    return (isinstance(value, (builtins.int, builtins.float))
            and not isinstance(value, builtins.bool))


def _ml_neg(value):
    if not _ml_is_number(value):
        raise TypeError(f"一元负号要求数字，实际为 {_ml_type(value)}")
    return -value


def _ml_str(value):
    if value is None:
        return "null"
    if isinstance(value, builtins.bool):
        return "true" if value else "false"
    if isinstance(value, MLList):
        return builtins.repr(value)
    if isinstance(value, builtins.float):
        return builtins.repr(value)
    return builtins.str(value)


def _ml_display(value):
    if isinstance(value, MLList):
        return "[" + ", ".join(_ml_display(item) for item in value) + "]"
    if value is None:
        return "null"
    if isinstance(value, builtins.bool):
        return "true" if value else "false"
    if isinstance(value, builtins.float):
        return f"{value:.1f}" if value == builtins.int(value) else builtins.repr(value)
    if isinstance(value, (_MLBuiltin, _MLUserFunction)):
        return builtins.repr(value)
    return builtins.str(value)


def _ml_print(*values):
    sys.stdout.write(" ".join(_ml_display(v) for v in values) + "\n")


def _ml_type(value):
    if value is None:
        return "null"
    if isinstance(value, builtins.bool):
        return "bool"
    if isinstance(value, builtins.int):
        return "int"
    if isinstance(value, builtins.float):
        return "float"
    if isinstance(value, builtins.str):
        return "string"
    if isinstance(value, MLList):
        return "list"
    if builtins.callable(value):
        return "function"
    return builtins.type(value).__name__


def _ml_add(left, right):
    if _ml_is_number(left) and _ml_is_number(right):
        return left + right
    if isinstance(left, builtins.str) or isinstance(right, builtins.str):
        return _ml_str(left) + _ml_str(right)
    if isinstance(left, MLList) and isinstance(right, MLList):
        return MLList(builtins.list(left) + builtins.list(right))
    raise TypeError(f"不能相加 {_ml_type(left)} 与 {_ml_type(right)}")


def _ml_sub(left, right):
    if not (_ml_is_number(left) and _ml_is_number(right)):
        raise TypeError(f"不能相减 {_ml_type(left)} 与 {_ml_type(right)}")
    return left - right


def _ml_mul(left, right):
    if _ml_is_number(left) and _ml_is_number(right):
        return left * right
    if (isinstance(left, builtins.str)
            and isinstance(right, builtins.int)
            and not isinstance(right, builtins.bool)):
        return left * right
    raise TypeError(f"不能相乘 {_ml_type(left)} 与 {_ml_type(right)}")


def _ml_div(left, right):
    if not (_ml_is_number(left) and _ml_is_number(right)):
        raise TypeError(f"不能相除 {_ml_type(left)} 与 {_ml_type(right)}")
    if right == 0:
        raise ZeroDivisionError("division by zero")
    return left / right


def _ml_mod(left, right):
    if not (_ml_is_number(left) and _ml_is_number(right)):
        raise TypeError(f"不能取模 {_ml_type(left)} 与 {_ml_type(right)}")
    if right == 0:
        raise ZeroDivisionError("integer division or modulo by zero")
    return left % right


def _ml_eq(left, right):
    if isinstance(left, MLList) and isinstance(right, MLList):
        return left is right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, builtins.bool) != isinstance(right, builtins.bool):
        return False
    return left == right


def _ml_ne(left, right):
    return not _ml_eq(left, right)


def _ml_check_ordering(left, right):
    strings = isinstance(left, builtins.str) and isinstance(right, builtins.str)
    numbers = (isinstance(left, (builtins.int, builtins.float))
               and isinstance(right, (builtins.int, builtins.float)))
    if not strings and not numbers:
        raise TypeError(f"不能比较 {_ml_type(left)} 与 {_ml_type(right)}")


def _ml_lt(left, right):
    _ml_check_ordering(left, right)
    return left < right


def _ml_le(left, right):
    _ml_check_ordering(left, right)
    return left <= right


def _ml_gt(left, right):
    _ml_check_ordering(left, right)
    return left > right


def _ml_ge(left, right):
    _ml_check_ordering(left, right)
    return left >= right


def _ml_index(target, index):
    if not isinstance(target, (MLList, builtins.str)):
        raise TypeError(f"下标访问要求列表或字符串，实际为 {_ml_type(target)}")
    if not isinstance(index, builtins.int) or isinstance(index, builtins.bool):
        raise TypeError(f"下标必须是 int，实际为 {_ml_type(index)}")
    if index < 0:
        index += builtins.len(target)
    if index < 0 or index >= builtins.len(target):
        raise IndexError(f"下标 {index} 越界，长度为 {builtins.len(target)}")
    return target[index]


def _ml_set_index(target, index, value):
    if not isinstance(target, MLList):
        raise TypeError(f"下标赋值要求列表，实际为 {_ml_type(target)}")
    if not isinstance(index, builtins.int) or isinstance(index, builtins.bool):
        raise TypeError(f"下标必须是 int，实际为 {_ml_type(index)}")
    if index < 0:
        index += builtins.len(target)
    if index < 0 or index >= builtins.len(target):
        raise IndexError(f"下标 {index} 越界，长度为 {builtins.len(target)}")
    target[index] = value


def _ml_index_update(target, index, value, op):
    old = _ml_index(target, index)
    _ml_set_index(target, index, _BINARY_OPS[op](old, value))


_BINARY_OPS = {
    "+": _ml_add,
    "-": _ml_sub,
    "*": _ml_mul,
    "/": _ml_div,
    "%": _ml_mod,
}


def _ml_len(value):
    if isinstance(value, (MLList, builtins.str)):
        return builtins.len(value)
    raise TypeError(f"len 要求列表或字符串，实际为 {_ml_type(value)}")


def _ml_push(values, value):
    if not isinstance(values, MLList):
        raise TypeError(f"push 要求列表，实际为 {_ml_type(values)}")
    values.append(value)
    return values


def _ml_pop(values):
    if not isinstance(values, MLList):
        raise TypeError(f"pop 要求列表，实际为 {_ml_type(values)}")
    return values.pop() if values else None


def _ml_int(value):
    if isinstance(value, builtins.bool):
        return 1 if value else 0
    if isinstance(value, builtins.int):
        return value
    if isinstance(value, builtins.float):
        return builtins.int(value)
    if isinstance(value, builtins.str):
        try:
            return builtins.int(value.strip())
        except ValueError:
            return 0
    return 0


def _ml_float(value):
    if isinstance(value, builtins.bool):
        return 1.0 if value else 0.0
    if isinstance(value, (builtins.int, builtins.float)):
        return builtins.float(value)
    if isinstance(value, builtins.str):
        try:
            return builtins.float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def _ml_range(start, stop=None, step=1):
    if stop is None:
        start, stop = 0, start
    return MLList(builtins.range(start, stop + 1, step))


def _ml_floor(value):
    return builtins.int(math.floor(value))


def _ml_ceil(value):
    return builtins.int(math.ceil(value))


def _ml_round(value, digits=None):
    # MiniLang 的 round(x) 保留 1 位小数；round(x, n) 按指定位数。
    return builtins.round(value, 1) if digits is None else builtins.round(value, digits)


def _ml_input():
    # Web VM 没有输入队列时返回空字符串，独立产物保持相同语义。
    return ""


def _ml_exit(code=0):
    sys.exit(code)


# MiniLang 内置名放在适配层末尾绑定，用户代码可直接使用 print/range/push 等名字。
print = _MLBuiltin("print", _ml_print)
len = _MLBuiltin("len", _ml_len)
push = _MLBuiltin("push", _ml_push)
pop = _MLBuiltin("pop", _ml_pop)
type = _MLBuiltin("type", _ml_type)
str = _MLBuiltin("str", _ml_str)
int = _MLBuiltin("int", _ml_int)
float = _MLBuiltin("float", _ml_float)
range = _MLBuiltin("range", _ml_range)
abs = _MLBuiltin("abs", builtins.abs)
min = _MLBuiltin("min", lambda *args: builtins.min(args))
max = _MLBuiltin("max", lambda *args: builtins.max(args))
sqrt = _MLBuiltin("sqrt", math.sqrt)
floor = _MLBuiltin("floor", _ml_floor)
ceil = _MLBuiltin("ceil", _ml_ceil)
round = _MLBuiltin("round", _ml_round)
input = _MLBuiltin("input", _ml_input)
time = _MLBuiltin("time", time.time)
random = _MLBuiltin("random", random.random)
exit = _MLBuiltin("exit", _ml_exit)
'''


_BUILTIN_NAMES = {
    "print": "print", "len": "len", "push": "push", "pop": "pop",
    "type": "type", "str": "str", "int": "int", "float": "float",
    "range": "range", "abs": "abs", "min": "min", "max": "max",
    "sqrt": "sqrt", "floor": "floor", "ceil": "ceil", "round": "round",
    "input": "input", "time": "time", "random": "random", "exit": "exit",
}

_COMPARE_OPS = {
    "==": "_ml_eq", "!=": "_ml_ne", "<": "_ml_lt",
    "<=": "_ml_le", ">": "_ml_gt", ">=": "_ml_ge",
}

_ARITHMETIC_OPS = {
    "+": "_ml_add", "-": "_ml_sub", "*": "_ml_mul",
    "/": "_ml_div", "%": "_ml_mod",
}


class PythonTranspiler:
    """遍历语义分析后的 AST，生成可直接执行的 Python 源码。"""

    def __init__(self, compile_result):
        self.result = compile_result
        self.program = compile_result.ast
        self.var_names = {}
        self.function_names = {}
        self.function_wrapper_names = {}
        self.lines = []
        self.loop_states = []
        self._prepare_names()

    # ------------------------------------------------------------------
    # 符号命名：每个语义符号使用唯一名，从名称层面消除块级遮蔽
    # ------------------------------------------------------------------
    def _prepare_names(self):
        variable_index = 0
        function_index = 0
        for symbol in self.result.symbol_table.all_symbols():
            if symbol.kind == sym.KIND_BUILTIN:
                continue
            if symbol.kind == sym.KIND_FUNCTION:
                function_index += 1
                self.function_names[symbol] = f"_ml_fn_{function_index}"
                self.function_wrapper_names[symbol] = f"_ml_callable_{function_index}"
            else:
                variable_index += 1
                self.var_names[symbol] = f"_ml_v_{variable_index}"

    def _symbol_name(self, symbol):
        if symbol is None:
            raise TranspileError("表达式缺少语义分析绑定的符号")
        if symbol.kind == sym.KIND_BUILTIN:
            return _BUILTIN_NAMES.get(symbol.name, symbol.name)
        if symbol.kind == sym.KIND_FUNCTION:
            name = self.function_wrapper_names.get(symbol)
        else:
            name = self.var_names.get(symbol)
        if not name:
            raise TranspileError(f"符号 {symbol.name!r} 缺少转译名称")
        return name

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def generate(self) -> str:
        self._validate_program()
        self.lines = [_RUNTIME_PREAMBLE.rstrip(), ""]

        functions = [d for d in self.program.declarations if isinstance(d, ast.FunctionDecl)]
        top_level_vars = [
            d.symbol for d in self.program.declarations
            if isinstance(d, ast.VarDecl) and d.symbol is not None
        ]

        for symbol in top_level_vars:
            self.lines.append(f"{self.var_names[symbol]} = _ML_UNDEFINED")
        if top_level_vars:
            self.lines.append("")

        for fn in functions:
            self._generate_function(fn)
            self.lines.append("")

        for fn in functions:
            implementation = self.function_names[fn.symbol]
            wrapper = self.function_wrapper_names[fn.symbol]
            self.lines.append(
                f"{wrapper} = _MLUserFunction({fn.name!r}, {fn.params!r}, {implementation})"
            )
        if functions:
            self.lines.append("")

        for decl in self.program.declarations:
            self._generate_stmt(decl, 0)
        return "\n".join(self.lines).rstrip() + "\n"

    # ------------------------------------------------------------------
    # 函数
    # ------------------------------------------------------------------
    def _function_scope(self, fn: ast.FunctionDecl):
        for child in fn.symbol.scope.children:
            if child.scope_type == sym.SCOPE_FUNCTION and child.name == fn.name:
                return child
        raise TranspileError(f"函数 {fn.name!r} 缺少函数作用域")

    def _generate_function(self, fn: ast.FunctionDecl):
        function_scope = self._function_scope(fn)
        param_symbols = [function_scope.symbols[name] for name in fn.params]
        params = [self.var_names[s] for s in param_symbols]
        name = self.function_names[fn.symbol]
        self.lines.append(f"def {name}({', '.join(params)}):")

        body_scope = function_scope.children[0] if function_scope.children else None
        direct_locals = [
            stmt.symbol for stmt in fn.body.statements
            if isinstance(stmt, ast.VarDecl) and stmt.symbol is not None
            and (body_scope is None or stmt.symbol.scope is body_scope)
        ]
        assigned_globals = self._collect_assigned_globals(fn.body)

        indent = 1
        if assigned_globals:
            self.emit(
                "global " + ", ".join(sorted(self.var_names[s] for s in assigned_globals)),
                indent,
            )
        for symbol in sorted(direct_locals, key=lambda s: self.var_names[s]):
            self.emit(f"{self.var_names[symbol]} = _ML_UNDEFINED", indent)
        self._generate_body(fn.body, indent)

    def _collect_assigned_globals(self, block: ast.Block):
        global_scope = self.result.symbol_table.global_scope
        found = set()

        def visit(stmt):
            if isinstance(stmt, ast.FunctionDecl):
                return
            assignment = None
            if isinstance(stmt, ast.ExprStmt) and isinstance(stmt.expr, ast.AssignStmt):
                assignment = stmt.expr
            elif isinstance(stmt, ast.AssignStmt):
                assignment = stmt
            if (isinstance(assignment, ast.AssignStmt)
                    and isinstance(assignment.target, ast.Identifier)
                    and assignment.target.symbol is not None
                    and assignment.target.symbol.scope is global_scope):
                found.add(assignment.target.symbol)
            for child in self._child_statements(stmt):
                visit(child)

        for stmt in block.statements:
            visit(stmt)
        return found

    def _child_statements(self, stmt):
        if isinstance(stmt, ast.Block):
            return tuple(stmt.statements)
        if isinstance(stmt, ast.IfStmt):
            children = []
            for _, body in stmt.branches:
                children.extend(body.statements)
            if stmt.else_block:
                children.extend(stmt.else_block.statements)
            return tuple(children)
        if isinstance(stmt, ast.WhileStmt):
            return tuple(stmt.body.statements)
        if isinstance(stmt, ast.ForStmt):
            children = [stmt.init] if stmt.init else []
            children.extend(stmt.body.statements)
            return tuple(children)
        return ()

    # ------------------------------------------------------------------
    # 语句
    # ------------------------------------------------------------------
    def emit(self, text, indent):
        self.lines.append("    " * indent + text)

    def _generate_body(self, block: ast.Block, indent):
        if block.statements:
            for stmt in block.statements:
                self._generate_stmt(stmt, indent)
        else:
            self.emit("pass", indent)

    def _generate_stmt(self, stmt, indent):
        if stmt is None or isinstance(stmt, ast.FunctionDecl):
            return
        if isinstance(stmt, ast.Block):
            self._generate_body(stmt, indent)
        elif isinstance(stmt, ast.VarDecl):
            target = self.var_names[stmt.symbol]
            value = self._expr(stmt.initializer) if stmt.initializer else "None"
            self.emit(f"{target} = {value}", indent)
        elif isinstance(stmt, ast.AssignStmt):
            self._assign(stmt, indent)
        elif isinstance(stmt, ast.ExprStmt):
            if isinstance(stmt.expr, ast.AssignStmt):
                self._assign(stmt.expr, indent)
            else:
                self.emit(self._expr(stmt.expr), indent)
        elif isinstance(stmt, ast.PrintStmt):
            args = ", ".join(self._expr(arg) for arg in stmt.args)
            self.emit(f"print({args})", indent)
        elif isinstance(stmt, ast.IfStmt):
            self._if(stmt, indent)
        elif isinstance(stmt, ast.WhileStmt):
            self._while(stmt, indent)
        elif isinstance(stmt, ast.ForStmt):
            self._for(stmt, indent)
        elif isinstance(stmt, ast.ReturnStmt):
            value = self._expr(stmt.value) if stmt.value else "None"
            self.emit(f"return {value}", indent)
        elif isinstance(stmt, ast.BreakStmt):
            if not self.loop_states:
                raise TranspileError("break 不在循环中")
            state = self.loop_states[-1]
            self.emit(f"{state} = 2", indent)
            self.emit("break", indent)
        elif isinstance(stmt, ast.ContinueStmt):
            if not self.loop_states:
                raise TranspileError("continue 不在循环中")
            state = self.loop_states[-1]
            self.emit(f"{state} = 1", indent)
            self.emit("break", indent)

    def _assign(self, stmt: ast.AssignStmt, indent):
        if isinstance(stmt.target, ast.Identifier):
            target = self.var_names.get(stmt.target.symbol)
            if not target:
                raise TranspileError(f"标识符 {stmt.target.name!r} 缺少语义分析符号")
            value = self._expr(stmt.value)
            if stmt.op == "=":
                self.emit(f"{target} = {value}", indent)
            else:
                current = self._identifier(stmt.target)
                helper = _ARITHMETIC_OPS[stmt.op[0]]
                self.emit(f"{target} = {helper}({current}, {value})", indent)
            return

        target = self._expr(stmt.target.target)
        index = self._expr(stmt.target.index)
        value = self._expr(stmt.value)
        if stmt.op == "=":
            self.emit(f"_ml_set_index({target}, {index}, {value})", indent)
        else:
            self.emit(
                f"_ml_index_update({target}, {index}, {value}, {stmt.op[0]!r})",
                indent,
            )

    def _if(self, stmt: ast.IfStmt, indent):
        first = True
        for condition, body in stmt.branches:
            self.emit(
                f"{'if' if first else 'elif'} _ml_truth({self._expr(condition)}):",
                indent,
            )
            self._generate_body(body, indent + 1)
            first = False
        if stmt.else_block:
            self.emit("else:", indent)
            self._generate_body(stmt.else_block, indent + 1)

    def _while(self, stmt: ast.WhileStmt, indent):
        state = self._begin_loop_state()
        self.emit(f"{state} = 0", indent)
        self.emit(f"while _ml_truth({self._expr(stmt.condition)}):", indent)
        self.emit(f"{state} = 0", indent + 1)
        self._emit_loop_body(stmt.body, indent + 1)
        self.emit(f"if {state} == 1:", indent + 1)
        self.emit("continue", indent + 2)
        self.emit(f"if {state} == 2:", indent + 1)
        self.emit("break", indent + 2)
        self.loop_states.pop()

    def _for(self, stmt: ast.ForStmt, indent):
        state = self._begin_loop_state()
        if stmt.init:
            self._generate_stmt(stmt.init, indent)
        condition = self._expr(stmt.condition) if stmt.condition else "True"
        self.emit("while True:", indent)
        self.emit(f"if not _ml_truth({condition}):", indent + 1)
        self.emit("break", indent + 2)
        self.emit(f"{state} = 0", indent + 1)
        self._emit_loop_body(stmt.body, indent + 1)
        # MiniLang 的 continue 仍会执行 for 的增量表达式。
        self.emit(f"if {state} == 2:", indent + 1)
        self.emit("break", indent + 2)
        if stmt.increment:
            if isinstance(stmt.increment, ast.AssignStmt):
                self._assign(stmt.increment, indent + 1)
            else:
                self.emit(self._expr(stmt.increment), indent + 1)
        self.loop_states.pop()

    def _begin_loop_state(self):
        state = f"_ml_loop_state_{len(self.loop_states) + 1}"
        self.loop_states.append(state)
        return state

    def _emit_loop_body(self, body: ast.Block, indent):
        # 内层 while 只用于接住 break/continue，并把控制原因写入状态变量。
        self.emit("while True:", indent)
        if body.statements:
            for stmt in body.statements:
                self._generate_stmt(stmt, indent + 1)
        self.emit("break", indent + 1)

    # ------------------------------------------------------------------
    # 表达式
    # ------------------------------------------------------------------
    def _expr(self, node) -> str:
        if node is None:
            return "None"
        if isinstance(node, ast.NumberLiteral):
            return builtins.repr(node.value)
        if isinstance(node, ast.StringLiteral):
            return builtins.repr(node.value)
        if isinstance(node, ast.BoolLiteral):
            return "True" if node.value else "False"
        if isinstance(node, ast.NullLiteral):
            return "None"
        if isinstance(node, ast.Identifier):
            return self._identifier(node)
        if isinstance(node, ast.UnaryExpr):
            if node.op == "!":
                return f"_ml_not({self._expr(node.operand)})"
            return f"_ml_neg({self._expr(node.operand)})"
        if isinstance(node, ast.BinaryExpr):
            helper = _COMPARE_OPS.get(node.op) or _ARITHMETIC_OPS.get(node.op)
            if not helper:
                raise TranspileError(f"暂不支持转译运算符 {node.op!r}")
            return f"{helper}({self._expr(node.left)}, {self._expr(node.right)})"
        if isinstance(node, ast.LogicalExpr):
            return self._logical(node)
        if isinstance(node, ast.CallExpr):
            args = ", ".join(self._expr(arg) for arg in node.args)
            return f"{self._expr(node.callee)}({args})"
        if isinstance(node, ast.IndexExpr):
            return f"_ml_index({self._expr(node.target)}, {self._expr(node.index)})"
        if isinstance(node, ast.ListLiteral):
            return "_ml_list(" + ", ".join(self._expr(e) for e in node.elements) + ")"
        raise TranspileError(f"暂不支持转译的 AST 节点：{type(node).__name__}")

    def _logical(self, node: ast.LogicalExpr):
        left = self._expr(node.left)
        right = self._expr(node.right)
        # lambda 保证左值只求值一次，并保留 && / || 的短路规则。
        if node.op == "&&":
            return (f"(lambda _ml_left: ({right}) "
                    f"if _ml_truth(_ml_left) else _ml_left)({left})")
        return (f"(lambda _ml_left: _ml_left "
                f"if _ml_truth(_ml_left) else ({right}))({left})")

    def _identifier(self, node: ast.Identifier) -> str:
        name = self._symbol_name(node.symbol)
        if node.symbol.kind == sym.KIND_VARIABLE:
            if node.symbol.scope is self.result.symbol_table.global_scope:
                return f"_ml_global({name!r})"
            return f"_ml_value({name!r}, {name})"
        return name

    # ------------------------------------------------------------------
    # 转译前校验：编译前端可接受、但现有字节码后端/本转译器不支持的结构
    # ------------------------------------------------------------------
    def _validate_program(self):
        for decl in self.program.declarations:
            if isinstance(decl, ast.FunctionDecl):
                self._validate_statements(decl.body.statements)
            else:
                self._validate_stmt(decl)

    def _validate_statements(self, statements):
        for stmt in statements:
            self._validate_stmt(stmt)

    def _validate_stmt(self, stmt):
        if stmt is None:
            return
        if isinstance(stmt, ast.FunctionDecl):
            self._unsupported("暂不支持嵌套函数声明；请把函数定义移动到顶层",
                              stmt.line, stmt.column)
        if isinstance(stmt, ast.Block):
            self._validate_statements(stmt.statements)
        elif isinstance(stmt, ast.VarDecl):
            if stmt.initializer:
                self._validate_expr(stmt.initializer)
        elif isinstance(stmt, ast.AssignStmt):
            self._validate_assignment(stmt)
        elif isinstance(stmt, ast.ExprStmt):
            if isinstance(stmt.expr, ast.AssignStmt):
                self._validate_assignment(stmt.expr)
            else:
                self._validate_expr(stmt.expr)
        elif isinstance(stmt, ast.PrintStmt):
            for arg in stmt.args:
                self._validate_expr(arg)
        elif isinstance(stmt, ast.IfStmt):
            for condition, body in stmt.branches:
                self._validate_expr(condition)
                self._validate_stmt(body)
            if stmt.else_block:
                self._validate_stmt(stmt.else_block)
        elif isinstance(stmt, ast.WhileStmt):
            self._validate_expr(stmt.condition)
            self._validate_stmt(stmt.body)
        elif isinstance(stmt, ast.ForStmt):
            if stmt.init:
                self._validate_stmt(stmt.init)
            if stmt.condition:
                self._validate_expr(stmt.condition)
            if stmt.increment:
                if isinstance(stmt.increment, ast.AssignStmt):
                    self._validate_assignment(stmt.increment)
                else:
                    self._validate_expr(stmt.increment)
            self._validate_stmt(stmt.body)
        elif isinstance(stmt, ast.ReturnStmt) and stmt.value:
            self._validate_expr(stmt.value)

    def _validate_assignment(self, stmt: ast.AssignStmt):
        self._validate_expr(stmt.value)
        target = stmt.target
        while isinstance(target, ast.IndexExpr):
            self._validate_expr(target.index)
            target = target.target
        if not isinstance(target, ast.Identifier):
            self._unsupported("赋值目标只能是变量或列表下标", stmt.line, stmt.column)

    def _validate_expr(self, expr):
        if expr is None or isinstance(expr, (
                ast.NumberLiteral, ast.StringLiteral, ast.BoolLiteral,
                ast.NullLiteral, ast.Identifier)):
            return
        if isinstance(expr, ast.AssignStmt):
            self._unsupported(
                "赋值表达式只能作为独立语句或 for 循环的更新部分",
                expr.line, expr.column,
            )
        if isinstance(expr, ast.UnaryExpr):
            self._validate_expr(expr.operand)
        elif isinstance(expr, (ast.BinaryExpr, ast.LogicalExpr)):
            self._validate_expr(expr.left)
            self._validate_expr(expr.right)
        elif isinstance(expr, ast.CallExpr):
            self._validate_expr(expr.callee)
            for arg in expr.args:
                self._validate_expr(arg)
        elif isinstance(expr, ast.IndexExpr):
            self._validate_expr(expr.target)
            self._validate_expr(expr.index)
        elif isinstance(expr, ast.ListLiteral):
            for element in expr.elements:
                self._validate_expr(element)

    def _unsupported(self, message, line, column):
        diagnostic = {
            "severity": "error",
            "phase": "semantic",
            "kind": "syntax",
            "message": message,
            "line": line,
            "column": column,
            "length": 1,
            "fix": "调整源码结构后再进行 Python 转译。",
        }
        raise TranspileError(message, diagnostic)


class TranspileError(Exception):
    """转译器内部错误：通常表示 AST 与语义分析结果不一致。"""

    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


def transpile(compile_result) -> str:
    """便捷入口：把成功编译的 CompileResult 转成 Python 源码。"""
    if not getattr(compile_result, "success", False):
        raise TranspileError("只有编译成功的程序才能转译")
    return PythonTranspiler(compile_result).generate()
