# Port of github.com/ianlancetaylor/demangle at version
# v0.0.0-20250628045327-2d64ad6b7ec5 (BSD-3-Clause, see LICENSE in this
# directory).  This module corresponds to ast.go.
#
# Only what demangle.ToString needs with *default options* is ported:
#   * the LLVMStyle, NoTemplateParams, NoEnclosingParams and MaxLength
#     printing options are not supported, so the branches that only run
#     with them are omitted (and with them the operator precedence
#     machinery and the printState.scopes counter, which are only read
#     in LLVM style);
#   * the GoString/goString debugging methods are omitted.
# Everything else follows ast.go type by type and method by method,
# including its quirks (for example VendorQualifier.copy keeping the old
# Type, or the writes through fmt.Fprintf that do not update
# printState.last), so the output is byte-for-byte identical.
#
# Strings are handled as "byte strings stored in str": the public entry
# point decodes the UTF-8 input as Latin-1 so that every character is
# one byte, exactly like Go string indexing.
"""Abstract syntax tree for demangled C++ names, and its printer."""

from __future__ import annotations

from typing import Callable, List, Optional

from .errors import InternalError

CopyFn = Callable[["AST"], Optional["AST"]]
SkipFn = Callable[["AST"], bool]
TraverseFn = Callable[["AST"], bool]


class AST:
    """Base class of all AST nodes (Go's AST interface).

    Node classes never inherit from each other, so ``isinstance`` checks
    behave like Go type switches on concrete pointer types.  Nodes use
    identity equality and hashing, like Go pointers.

    Node types that implement Go's ``innerPrinter`` interface define a
    ``print_inner`` method; the base class deliberately does not.
    """

    __slots__ = ()

    def print(self, ps: "PrintState") -> None:  # pragma: no cover
        raise NotImplementedError

    def traverse(self, fn: TraverseFn) -> None:  # pragma: no cover
        raise NotImplementedError

    def copy(self, fn: CopyFn, skip: SkipFn) -> Optional["AST"]:  # pragma: no cover
        raise NotImplementedError


def _copy_list(lst: List[AST], fn: CopyFn, skip: SkipFn):
    """Copy each element of lst; return (new list, whether any changed)."""
    changed = False
    out = []
    for a in lst:
        ac = a.copy(fn, skip)
        if ac is None:
            out.append(a)
        else:
            out.append(ac)
            changed = True
    return out, changed


def ast_to_string(a: AST) -> str:
    """ASTToString with default options."""
    ps = PrintState()
    a.print(ps)
    return "".join(ps.buf)


class PrintState:
    """The printState type holds information needed to print an AST."""

    __slots__ = ("lambda_template_args", "buf", "last", "inner", "printing")

    def __init__(self) -> None:
        # lambda_template_args is true if we are printing the template
        # arguments to a lambda with explicit template parameters.
        # In that case template parameters are printed without names.
        self.lambda_template_args = False
        self.buf: List[str] = []
        # Last byte written to buffer ("" stands for Go's zero byte).
        self.last = ""
        # The inner field is a list of items to print for a type
        # name.  This is used by types to implement the inside-out
        # C++ declaration syntax.
        self.inner: List[AST] = []
        # The printing field counts the items we are currently
        # printing (Go keeps a list; we keep a count per node).  This
        # avoids endless recursion if a substitution reference creates
        # a cycle in the graph.
        self.printing: dict = {}

    def write_byte(self, b: str) -> None:
        self.last = b
        self.buf.append(b)

    def write_string(self, s: str) -> None:
        if s:
            self.last = s[-1]
            self.buf.append(s)

    def write_raw(self, s: str) -> None:
        """fmt.Fprintf(&ps.buf, ...): does not update ps.last."""
        self.buf.append(s)

    # startScope/endScope only maintain the scopes counter (LLVM style
    # only) besides writing the byte.
    start_scope = write_byte
    end_scope = write_byte

    def print(self, a: AST) -> None:
        """Print an AST."""
        printing = self.printing
        k = id(a)
        c = printing.get(k, 0)
        if c > 1:
            # We permit the type to appear once, and return without
            # printing anything if we see it twice.  This is for a
            # case like _Z6outer2IsEPFilES1_, where the substitution
            # is printed differently the second time because the set
            # of inner types is different.
            return
        printing[k] = c + 1
        a.print(self)
        printing[k] = c

    def print_list(self, args: List[AST], skip: Optional[Callable[[AST], bool]]) -> None:
        """Print a list of AST values separated by commas, optionally
        skipping some."""
        first = True
        for a in args:
            if skip is not None and skip(a):
                continue
            if not first:
                self.write_string(", ")
            self.print(a)
            first = False

    def print_inner(self, prefix_only: bool) -> List[AST]:
        """Print the inner types."""
        save: Optional[List[AST]] = [] if prefix_only else None
        while self.inner:
            self.print_one_inner(save)
        return save if save is not None else []

    def print_one_inner(self, save: Optional[List[AST]]) -> None:
        """Print the most recent inner type.  If save is not None, only
        print prefixes."""
        if not self.inner:
            raise InternalError("printOneInner called with no inner types")
        a = self.inner.pop()

        if save is not None and isinstance(a, MethodWithQualifiers):
            save.append(a)
            return

        pi = getattr(a, "print_inner", None)
        if pi is not None:
            pi(self)
        else:
            self.print(a)

    def is_empty(self, a: AST) -> bool:
        """Report whether printing a will not print anything."""
        if isinstance(a, ArgumentPack):
            for x in a.args:
                if not self.is_empty(x):
                    return False
            return True
        if isinstance(a, ExprList):
            return len(a.exprs) == 0
        if isinstance(a, PackExpansion):
            return a.pack is not None and self.is_empty(a.base)
        return False


class Name(AST):
    """Name is an unqualified name."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def print(self, ps):
        ps.write_string(self.name)

    def traverse(self, fn):
        fn(self)

    def copy(self, fn, skip):
        if skip(self):
            return None
        return fn(self)


class Typed(AST):
    """Typed is a typed name."""

    __slots__ = ("name", "type")

    def __init__(self, name: AST, type: AST) -> None:
        self.name = name
        self.type = type

    def print(self, ps):
        # We are printing a typed name, so ignore the current set of
        # inner names to print.  Pass down our name as the one to use.
        hold_inner = ps.inner
        ps.inner = [self]
        ps.print(self.type)
        if ps.inner:
            # The type did not print the name; print it now in
            # the default location.
            ps.write_byte(" ")
            ps.print(self.name)
        ps.inner = hold_inner

    def print_inner(self, ps):
        ps.print(self.name)

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)
            self.type.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        name = self.name.copy(fn, skip)
        typ = self.type.copy(fn, skip)
        if name is None and typ is None:
            return fn(self)
        if name is None:
            name = self.name
        if typ is None:
            typ = self.type
        t = Typed(name, typ)
        r = fn(t)
        return r if r is not None else t


class Qualified(AST):
    """Qualified is a name in a scope."""

    __slots__ = ("scope", "name", "local_name")

    def __init__(self, scope: AST, name: AST, local_name: bool = False) -> None:
        self.scope = scope
        self.name = name
        # The local_name field is true if this is parsed as a
        # <local-name>.  We shouldn't really need this, but in some
        # cases (for the unary sizeof operator) the standard
        # demangler prints a local name slightly differently.  We
        # keep track of this for compatibility.
        self.local_name = local_name

    def print(self, ps):
        ps.print(self.scope)
        ps.write_string("::")
        ps.print(self.name)

    def traverse(self, fn):
        if fn(self):
            self.scope.traverse(fn)
            self.name.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        scope = self.scope.copy(fn, skip)
        name = self.name.copy(fn, skip)
        if scope is None and name is None:
            return fn(self)
        if scope is None:
            scope = self.scope
        if name is None:
            name = self.name
        q = Qualified(scope, name, self.local_name)
        r = fn(q)
        return r if r is not None else q


class Template(AST):
    """Template is a template with arguments."""

    __slots__ = ("name", "args")

    def __init__(self, name: Optional[AST], args: List[AST]) -> None:
        self.name = name
        self.args = args

    def print(self, ps):
        # Inner types apply to the template as a whole, they don't
        # cross over into the template.
        hold_inner = ps.inner
        ps.inner = []

        ps.print(self.name)

        # We need an extra space after operator<.
        if ps.last == "<":
            ps.write_byte(" ")

        ps.write_byte("<")
        ps.print_list(self.args, ps.is_empty)
        if ps.last == ">":
            # Avoid syntactic ambiguity in old versions of C++.
            ps.write_byte(" ")
        ps.write_byte(">")

        ps.inner = hold_inner

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)
            for a in self.args:
                a.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        name = self.name.copy(fn, skip)
        changed = name is not None
        args, ch = _copy_list(self.args, fn, skip)
        if not (changed or ch):
            return fn(self)
        if name is None:
            name = self.name
        t = Template(name, args)
        r = fn(t)
        return r if r is not None else t


class TemplateParam(AST):
    """TemplateParam is a template parameter.  The Template field is
    filled in while parsing the demangled string.  We don't normally
    see these while printing--they are replaced by the simplify
    function."""

    __slots__ = ("index", "template")

    def __init__(self, index: int, template: Optional[Template]) -> None:
        self.index = index
        self.template = template

    def print(self, ps):
        if self.template is None:
            raise InternalError("TemplateParam Template field is nil")
        if self.index >= len(self.template.args):
            raise InternalError("TemplateParam Index out of bounds")
        ps.print(self.template.args[self.index])

    def traverse(self, fn):
        fn(self)
        # Don't traverse Template--it points elsewhere in the AST.

    def copy(self, fn, skip):
        if skip(self):
            return None
        return fn(self)


class LambdaAuto(AST):
    """LambdaAuto is a lambda auto parameter."""

    __slots__ = ("index",)

    def __init__(self, index: int) -> None:
        self.index = index

    def print(self, ps):
        # We print the index plus 1 because that is what the standard
        # demangler does.
        ps.write_raw("auto:%d" % (self.index + 1))

    def traverse(self, fn):
        fn(self)

    def copy(self, fn, skip):
        if skip(self):
            return None
        return fn(self)


class TemplateParamQualifiedArg(AST):
    """TemplateParamQualifiedArg is used when the mangled name includes
    both the template parameter declaration and the template argument.
    See https://github.com/itanium-cxx-abi/cxx-abi/issues/47."""

    __slots__ = ("param", "arg")

    def __init__(self, param: AST, arg: AST) -> None:
        self.param = param
        self.arg = arg

    def print(self, ps):
        # We only demangle the actual template argument.
        # That is what the LLVM demangler does.
        # The parameter disambiguates the argument,
        # but is hopefully not required by a human reader.
        ps.print(self.arg)

    def traverse(self, fn):
        if fn(self):
            self.param.traverse(fn)
            self.arg.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        param = self.param.copy(fn, skip)
        arg = self.arg.copy(fn, skip)
        if param is None and arg is None:
            return fn(self)
        if param is None:
            param = self.param
        if arg is None:
            arg = self.arg
        t = TemplateParamQualifiedArg(param, arg)
        r = fn(t)
        return r if r is not None else t


class Qualifiers(AST):
    """Qualifiers is an ordered list of type qualifiers."""

    __slots__ = ("qualifiers",)

    def __init__(self, qualifiers: List[AST]) -> None:
        self.qualifiers = qualifiers

    def print(self, ps):
        first = True
        for q in self.qualifiers:
            if not first:
                ps.write_byte(" ")
            q.print(ps)
            first = False

    def traverse(self, fn):
        if fn(self):
            for q in self.qualifiers:
                q.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        qualifiers, changed = _copy_list(self.qualifiers, fn, skip)
        if not changed:
            return fn(self)
        qs = Qualifiers(qualifiers)
        r = fn(qs)
        return r if r is not None else qs


class Qualifier(AST):
    """Qualifier is a single type qualifier."""

    __slots__ = ("name", "exprs")

    def __init__(self, name: str, exprs: Optional[List[AST]] = None) -> None:
        self.name = name  # qualifier name: const, volatile, etc.
        self.exprs = exprs if exprs is not None else []  # for noexcept and throw

    def print(self, ps):
        ps.write_string(self.name)
        if self.exprs:
            ps.start_scope("(")
            first = True
            for e in self.exprs:
                if isinstance(e, ExprList) and len(e.exprs) == 0:
                    continue
                if not first:
                    ps.write_string(", ")
                ps.print(e)
                first = False
            ps.end_scope(")")

    def traverse(self, fn):
        if fn(self):
            for e in self.exprs:
                e.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        exprs, changed = _copy_list(self.exprs, fn, skip)
        if not changed:
            return fn(self)
        q = Qualifier(self.name, exprs)
        r = fn(q)
        return r if r is not None else q


class TypeWithQualifiers(AST):
    """TypeWithQualifiers is a type with standard qualifiers."""

    __slots__ = ("base", "qualifiers")

    def __init__(self, base: AST, qualifiers: AST) -> None:
        self.base = base
        self.qualifiers = qualifiers

    def print(self, ps):
        # Give the base type a chance to print the inner types.
        ps.inner.append(self)
        ps.print(self.base)
        if ps.inner:
            # The qualifier wasn't printed by Base.
            ps.write_byte(" ")
            ps.print(self.qualifiers)
            ps.inner.pop()

    def print_inner(self, ps):
        # Print qualifiers as an inner type by just printing the qualifiers.
        ps.write_byte(" ")
        ps.print(self.qualifiers)

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        quals = self.qualifiers.copy(fn, skip)
        if base is None and quals is None:
            return fn(self)
        if base is None:
            base = self.base
        if quals is None:
            quals = self.qualifiers
        t = TypeWithQualifiers(base, quals)
        r = fn(t)
        return r if r is not None else t


class MethodWithQualifiers(AST):
    """MethodWithQualifiers is a method with qualifiers."""

    __slots__ = ("method", "qualifiers", "ref_qualifier")

    def __init__(self, method: AST, qualifiers: Optional[AST], ref_qualifier: str) -> None:
        self.method = method
        self.qualifiers = qualifiers
        self.ref_qualifier = ref_qualifier  # "" or "&" or "&&"

    def print(self, ps):
        # Give the base type a chance to print the inner types.
        ps.inner.append(self)
        ps.print(self.method)
        if ps.inner:
            if self.qualifiers is not None:
                ps.write_byte(" ")
                ps.print(self.qualifiers)
            if self.ref_qualifier:
                ps.write_byte(" ")
                ps.write_string(self.ref_qualifier)
            ps.inner.pop()

    def print_inner(self, ps):
        if self.qualifiers is not None:
            ps.write_byte(" ")
            ps.print(self.qualifiers)
        if self.ref_qualifier:
            ps.write_byte(" ")
            ps.write_string(self.ref_qualifier)

    def traverse(self, fn):
        if fn(self):
            self.method.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        method = self.method.copy(fn, skip)
        quals = None
        if self.qualifiers is not None:
            quals = self.qualifiers.copy(fn, skip)
        if method is None and quals is None:
            return fn(self)
        if method is None:
            method = self.method
        if quals is None:
            quals = self.qualifiers
        m = MethodWithQualifiers(method, quals, self.ref_qualifier)
        r = fn(m)
        return r if r is not None else m


class BuiltinType(AST):
    """BuiltinType is a builtin type, like "int"."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def print(self, ps):
        ps.write_string(self.name)

    def traverse(self, fn):
        fn(self)

    def copy(self, fn, skip):
        if skip(self):
            return None
        return fn(self)


def print_base(ps: PrintState, qual: AST, base: AST) -> None:
    """printBase is common print code for types that are printed with a
    simple suffix."""
    ps.inner.append(qual)
    ps.print(base)
    if ps.inner:
        qual.print_inner(ps)
        ps.inner.pop()


class PointerType(AST):
    """PointerType is a pointer type."""

    __slots__ = ("base",)

    def __init__(self, base: AST) -> None:
        self.base = base

    def print(self, ps):
        print_base(ps, self, self.base)

    def print_inner(self, ps):
        ps.write_string("*")

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = PointerType(base)
        r = fn(t)
        return r if r is not None else t


class ReferenceType(AST):
    """ReferenceType is a reference type."""

    __slots__ = ("base",)

    def __init__(self, base: AST) -> None:
        self.base = base

    def print(self, ps):
        print_base(ps, self, self.base)

    def print_inner(self, ps):
        ps.write_string("&")

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = ReferenceType(base)
        r = fn(t)
        return r if r is not None else t


class RvalueReferenceType(AST):
    """RvalueReferenceType is an rvalue reference type."""

    __slots__ = ("base",)

    def __init__(self, base: AST) -> None:
        self.base = base

    def print(self, ps):
        print_base(ps, self, self.base)

    def print_inner(self, ps):
        ps.write_string("&&")

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = RvalueReferenceType(base)
        r = fn(t)
        return r if r is not None else t


class ComplexType(AST):
    """ComplexType is a complex type."""

    __slots__ = ("base",)

    def __init__(self, base: AST) -> None:
        self.base = base

    def print(self, ps):
        print_base(ps, self, self.base)

    def print_inner(self, ps):
        ps.write_string(" _Complex")

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = ComplexType(base)
        r = fn(t)
        return r if r is not None else t


class ImaginaryType(AST):
    """ImaginaryType is an imaginary type."""

    __slots__ = ("base",)

    def __init__(self, base: AST) -> None:
        self.base = base

    def print(self, ps):
        print_base(ps, self, self.base)

    def print_inner(self, ps):
        ps.write_string(" _Imaginary")

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = ImaginaryType(base)
        r = fn(t)
        return r if r is not None else t


class SuffixType(AST):
    """SuffixType is an type with an arbitrary suffix."""

    __slots__ = ("base", "suffix")

    def __init__(self, base: AST, suffix: str) -> None:
        self.base = base
        self.suffix = suffix

    def print(self, ps):
        print_base(ps, self, self.base)

    def print_inner(self, ps):
        ps.write_byte(" ")
        ps.write_string(self.suffix)

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = SuffixType(base, self.suffix)
        r = fn(t)
        return r if r is not None else t


class TransformedType(AST):
    """TransformedType is a builtin type with a template argument."""

    __slots__ = ("name", "base")

    def __init__(self, name: str, base: AST) -> None:
        self.name = name
        self.base = base

    def print(self, ps):
        ps.write_string(self.name)
        ps.start_scope("(")
        ps.print(self.base)
        ps.end_scope(")")

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = TransformedType(self.name, base)
        r = fn(t)
        return r if r is not None else t


class VendorQualifier(AST):
    """VendorQualifier is a type qualified by a vendor-specific qualifier."""

    __slots__ = ("qualifier", "type")

    def __init__(self, qualifier: AST, type: AST) -> None:
        self.qualifier = qualifier
        self.type = type

    def print(self, ps):
        ps.inner.append(self)
        ps.print(self.type)
        if ps.inner:
            ps.print_one_inner(None)

    def print_inner(self, ps):
        ps.write_byte(" ")
        ps.print(self.qualifier)

    def traverse(self, fn):
        if fn(self):
            self.qualifier.traverse(fn)
            self.type.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        qualifier = self.qualifier.copy(fn, skip)
        typ = self.type.copy(fn, skip)
        if qualifier is None and typ is None:
            return fn(self)
        if qualifier is None:
            qualifier = self.qualifier
        # Upstream builds the copy with the original Type, not typ.
        t = VendorQualifier(qualifier, self.type)
        r = fn(t)
        return r if r is not None else t


class ArrayType(AST):
    """ArrayType is an array type."""

    __slots__ = ("dimension", "element")

    def __init__(self, dimension: AST, element: AST) -> None:
        self.dimension = dimension
        self.element = element

    def print(self, ps):
        # Pass the array type down as an inner type so that we print
        # multi-dimensional arrays correctly.
        ps.inner.append(self)
        ps.print(self.element)
        if ps.inner:
            ps.inner.pop()
            self.print_dimension(ps)

    def print_inner(self, ps):
        self.print_dimension(ps)

    def print_dimension(self, ps):
        """Print the array dimension."""
        space = " "
        while ps.inner:
            # We haven't gotten to the real type yet.  Use
            # parentheses around that type, except that if it is
            # an array type we print it as a multi-dimensional
            # array
            in_ = ps.inner[-1]
            if isinstance(in_, TypeWithQualifiers):
                in_ = in_.base
            if isinstance(in_, ArrayType):
                if in_ is ps.inner[-1]:
                    space = ""
                ps.print_one_inner(None)
            else:
                ps.write_byte(" ")
                ps.start_scope("(")
                ps.print_inner(False)
                ps.end_scope(")")
        ps.write_string(space)
        ps.write_byte("[")
        ps.print(self.dimension)
        ps.write_byte("]")

    def traverse(self, fn):
        if fn(self):
            self.dimension.traverse(fn)
            self.element.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        dimension = self.dimension.copy(fn, skip)
        element = self.element.copy(fn, skip)
        if dimension is None and element is None:
            return fn(self)
        if dimension is None:
            dimension = self.dimension
        if element is None:
            element = self.element
        t = ArrayType(dimension, element)
        r = fn(t)
        return r if r is not None else t


class FunctionType(AST):
    """FunctionType is a function type."""

    __slots__ = ("ret", "args", "for_local_name")

    def __init__(self, ret: Optional[AST], args: List[AST], for_local_name: bool = False) -> None:
        self.ret = ret
        self.args = args
        # The for_local_name field reports whether this FunctionType
        # was created for a local name. With the default GNU demangling
        # output we don't print the return type in that case.
        self.for_local_name = for_local_name

    def print(self, ps):
        ret_type = self.ret
        if self.for_local_name:
            ret_type = None
        if ret_type is not None:
            # Pass the return type as an inner type in order to
            # print the arguments in the right location.
            ps.inner.append(self)
            ps.print(ret_type)
            if not ps.inner:
                # Everything was printed.
                return
            ps.inner.pop()
            ps.write_byte(" ")
        self.print_args(ps)

    def print_inner(self, ps):
        self.print_args(ps)

    def print_args(self, ps):
        """Print the arguments of a function type.  It looks at the
        inner types for spacing."""
        paren = False
        space = False
        inner = ps.inner
        for i in range(len(inner) - 1, -1, -1):
            t = inner[i]
            if isinstance(t, (PointerType, ReferenceType, RvalueReferenceType)):
                paren = True
            elif isinstance(t, (TypeWithQualifiers, ComplexType, ImaginaryType, PtrMem)):
                space = True
                paren = True
            if paren:
                break

        if paren:
            if not space and (ps.last != "(" and ps.last != "*"):
                space = True
            if space and ps.last != " ":
                ps.write_byte(" ")
            ps.start_scope("(")

        save = ps.print_inner(True)

        if paren:
            ps.end_scope(")")

        ps.start_scope("(")
        first = True
        for a in self.args:
            if ps.is_empty(a):
                continue
            if not first:
                ps.write_string(", ")
            ps.print(a)
            first = False
        ps.end_scope(")")

        ps.inner = save
        ps.print_inner(False)

    def traverse(self, fn):
        if fn(self):
            if self.ret is not None:
                self.ret.traverse(fn)
            for a in self.args:
                a.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        changed = False
        ret = None
        if self.ret is not None:
            ret = self.ret.copy(fn, skip)
            if ret is None:
                ret = self.ret
            else:
                changed = True
        args, ch = _copy_list(self.args, fn, skip)
        if not (changed or ch):
            return fn(self)
        t = FunctionType(ret, args, self.for_local_name)
        r = fn(t)
        return r if r is not None else t


class FunctionParam(AST):
    """FunctionParam is a parameter of a function, used for
    last-specified return type in a closure."""

    __slots__ = ("index",)

    def __init__(self, index: int) -> None:
        self.index = index

    def print(self, ps):
        if self.index == 0:
            ps.write_string("this")
        else:
            ps.write_raw("{parm#%d}" % self.index)

    def traverse(self, fn):
        fn(self)

    def copy(self, fn, skip):
        if skip(self):
            return None
        return fn(self)


class PtrMem(AST):
    """PtrMem is a pointer-to-member expression."""

    __slots__ = ("class_", "member")

    def __init__(self, class_: AST, member: AST) -> None:
        self.class_ = class_
        self.member = member

    def print(self, ps):
        ps.inner.append(self)
        ps.print(self.member)
        if ps.inner:
            ps.print_one_inner(None)

    def print_inner(self, ps):
        if ps.last != "(":
            ps.write_byte(" ")
        ps.print(self.class_)
        ps.write_string("::*")

    def traverse(self, fn):
        if fn(self):
            self.class_.traverse(fn)
            self.member.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        class_ = self.class_.copy(fn, skip)
        member = self.member.copy(fn, skip)
        if class_ is None and member is None:
            return fn(self)
        if class_ is None:
            class_ = self.class_
        if member is None:
            member = self.member
        t = PtrMem(class_, member)
        r = fn(t)
        return r if r is not None else t


class FixedType(AST):
    """FixedType is a fixed numeric type of unknown size."""

    __slots__ = ("base", "accum", "sat")

    def __init__(self, base: AST, accum: bool, sat: bool) -> None:
        self.base = base
        self.accum = accum
        self.sat = sat

    def print(self, ps):
        if self.sat:
            ps.write_string("_Sat ")
        b = self.base
        if isinstance(b, BuiltinType) and b.name == "int":
            # The standard demangler skips printing "int".
            pass
        else:
            ps.print(b)
            ps.write_byte(" ")
        if self.accum:
            ps.write_string("_Accum")
        else:
            ps.write_string("_Fract")

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = FixedType(base, self.accum, self.sat)
        r = fn(t)
        return r if r is not None else t


class BinaryFP(AST):
    """BinaryFP is a binary floating-point type."""

    __slots__ = ("bits", "suffix")

    def __init__(self, bits: int, suffix: str) -> None:
        self.bits = bits
        self.suffix = suffix

    def print(self, ps):
        ps.write_raw("_Float%d%s" % (self.bits, self.suffix))

    def traverse(self, fn):
        fn(self)

    def copy(self, fn, skip):
        if skip(self):
            return None
        return fn(self)


class BitIntType(AST):
    """BitIntType is the C++23 _BitInt(N) type."""

    __slots__ = ("size", "signed")

    def __init__(self, size: AST, signed: bool) -> None:
        self.size = size
        self.signed = signed

    def print(self, ps):
        if not self.signed:
            ps.write_string("unsigned ")
        ps.write_string("_BitInt")
        ps.start_scope("(")
        ps.print(self.size)
        ps.end_scope(")")

    def traverse(self, fn):
        if fn(self):
            self.size.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        size = self.size.copy(fn, skip)
        if size is None:
            return fn(self)
        t = BitIntType(size, self.signed)
        r = fn(t)
        return r if r is not None else t


class VectorType(AST):
    """VectorType is a vector type."""

    __slots__ = ("dimension", "base")

    def __init__(self, dimension: AST, base: AST) -> None:
        self.dimension = dimension
        self.base = base

    def print(self, ps):
        ps.inner.append(self)
        ps.print(self.base)
        if ps.inner:
            ps.print_one_inner(None)

    def print_inner(self, ps):
        ps.write_string(" __vector(")
        ps.print(self.dimension)
        ps.write_byte(")")

    def traverse(self, fn):
        if fn(self):
            self.dimension.traverse(fn)
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        dimension = self.dimension.copy(fn, skip)
        base = self.base.copy(fn, skip)
        if dimension is None and base is None:
            return fn(self)
        if dimension is None:
            dimension = self.dimension
        if base is None:
            base = self.base
        t = VectorType(dimension, base)
        r = fn(t)
        return r if r is not None else t


class ElaboratedType(AST):
    """ElaboratedType is an elaborated struct/union/enum type."""

    __slots__ = ("kind", "type")

    def __init__(self, kind: str, type: AST) -> None:
        self.kind = kind
        self.type = type

    def print(self, ps):
        ps.write_string(self.kind)
        ps.write_string(" ")
        self.type.print(ps)

    def traverse(self, fn):
        if fn(self):
            self.type.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        typ = self.type.copy(fn, skip)
        if typ is None:
            return fn(self)
        t = ElaboratedType(self.kind, typ)
        r = fn(t)
        return r if r is not None else t


class Decltype(AST):
    """Decltype is the decltype operator."""

    __slots__ = ("expr",)

    def __init__(self, expr: AST) -> None:
        self.expr = expr

    def print(self, ps):
        ps.write_string("decltype")
        ps.write_string(" ")
        ps.start_scope("(")
        ps.print(self.expr)
        ps.end_scope(")")

    def traverse(self, fn):
        if fn(self):
            self.expr.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        expr = self.expr.copy(fn, skip)
        if expr is None:
            return fn(self)
        t = Decltype(expr)
        r = fn(t)
        return r if r is not None else t


class Operator(AST):
    """Operator is an operator.

    (The Go type also records the operator precedence, which is only
    used for LLVM style output.)"""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def print(self, ps):
        ps.write_string("operator")
        c = self.name[0]  # IndexError for "", like the Go index panic
        if "a" <= c <= "z":
            ps.write_byte(" ")
        n = self.name
        if n.endswith(" "):
            n = n[:-1]
        ps.write_string(n)

    def traverse(self, fn):
        fn(self)

    def copy(self, fn, skip):
        if skip(self):
            return None
        return fn(self)


class Constructor(AST):
    """Constructor is a constructor."""

    __slots__ = ("name", "base")

    def __init__(self, name: AST, base: Optional[AST]) -> None:
        self.name = name
        self.base = base  # base class of inheriting constructor

    def print(self, ps):
        ps.print(self.name)
        # We don't include the base class in the demangled string.

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)
            if self.base is not None:
                self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        name = self.name.copy(fn, skip)
        base = None
        if self.base is not None:
            base = self.base.copy(fn, skip)
        if name is None and base is None:
            return fn(self)
        if name is None:
            name = self.name
        if base is None:
            base = self.base
        t = Constructor(name, base)
        r = fn(t)
        return r if r is not None else t


class Destructor(AST):
    """Destructor is a destructor."""

    __slots__ = ("name",)

    def __init__(self, name: AST) -> None:
        self.name = name

    def print(self, ps):
        ps.write_byte("~")
        ps.print(self.name)

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        name = self.name.copy(fn, skip)
        if name is None:
            return fn(self)
        t = Destructor(name)
        r = fn(t)
        return r if r is not None else t


class GlobalCDtor(AST):
    """GlobalCDtor is a global constructor or destructor."""

    __slots__ = ("ctor", "key")

    def __init__(self, ctor: bool, key: AST) -> None:
        self.ctor = ctor
        self.key = key

    def print(self, ps):
        ps.write_string("global ")
        if self.ctor:
            ps.write_string("constructors")
        else:
            ps.write_string("destructors")
        ps.write_string(" keyed to ")
        ps.print(self.key)

    def traverse(self, fn):
        if fn(self):
            self.key.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        key = self.key.copy(fn, skip)
        if key is None:
            return fn(self)
        t = GlobalCDtor(self.ctor, key)
        r = fn(t)
        return r if r is not None else t


class TaggedName(AST):
    """TaggedName is a name with an ABI tag."""

    __slots__ = ("name", "tag")

    def __init__(self, name: AST, tag: AST) -> None:
        self.name = name
        self.tag = tag

    def print(self, ps):
        ps.print(self.name)
        ps.write_string("[abi:")
        ps.print(self.tag)
        ps.write_byte("]")

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)
            self.tag.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        name = self.name.copy(fn, skip)
        tag = self.tag.copy(fn, skip)
        if name is None and tag is None:
            return fn(self)
        if name is None:
            name = self.name
        if tag is None:
            tag = self.tag
        t = TaggedName(name, tag)
        r = fn(t)
        return r if r is not None else t


class PackExpansion(AST):
    """PackExpansion is a pack expansion.  The Pack field may be None."""

    __slots__ = ("base", "pack")

    def __init__(self, base: AST, pack: Optional["ArgumentPack"]) -> None:
        self.base = base
        self.pack = pack

    def print(self, ps):
        # We normally only get here if the simplify function was
        # unable to locate and expand the pack.
        if self.pack is None:
            parenthesize(ps, self.base)
            ps.write_string("...")
        else:
            ps.print(self.base)

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)
            # Don't traverse Template--it points elsewhere in the AST.

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = PackExpansion(base, self.pack)
        r = fn(t)
        return r if r is not None else t


class ArgumentPack(AST):
    """ArgumentPack is an argument pack."""

    __slots__ = ("args",)

    def __init__(self, args: List[AST]) -> None:
        self.args = args

    def print(self, ps):
        for i, a in enumerate(self.args):
            if i > 0:
                ps.write_string(", ")
            ps.print(a)

    def traverse(self, fn):
        if fn(self):
            for a in self.args:
                a.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        args, changed = _copy_list(self.args, fn, skip)
        if not changed:
            return fn(self)
        t = ArgumentPack(args)
        r = fn(t)
        return r if r is not None else t


class SizeofPack(AST):
    """SizeofPack is the sizeof operator applied to an argument pack."""

    __slots__ = ("pack",)

    def __init__(self, pack: ArgumentPack) -> None:
        self.pack = pack

    def print(self, ps):
        ps.write_string("%d" % len(self.pack.args))

    def traverse(self, fn):
        fn(self)
        # Don't traverse the pack--it points elsewhere in the AST.

    def copy(self, fn, skip):
        if skip(self):
            return None
        t = SizeofPack(self.pack)
        r = fn(t)
        return r if r is not None else t


class SizeofArgs(AST):
    """SizeofArgs is the size of a captured template parameter pack from
    an alias template."""

    __slots__ = ("args",)

    def __init__(self, args: List[AST]) -> None:
        self.args = args

    def print(self, ps):
        c = 0
        for a in self.args:
            if isinstance(a, ArgumentPack):
                c += len(a.args)
            elif isinstance(a, ExprList):
                c += len(a.exprs)
            else:
                c += 1
        ps.write_string("%d" % c)

    def traverse(self, fn):
        if fn(self):
            for a in self.args:
                a.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        args, changed = _copy_list(self.args, fn, skip)
        if not changed:
            return fn(self)
        t = SizeofArgs(args)
        r = fn(t)
        return r if r is not None else t


class TemplateParamName(AST):
    """TemplateParamName is the name of a template parameter that the
    demangler introduced for a lambda that has explicit template
    parameters.  This is a prefix with an index."""

    __slots__ = ("prefix", "index")

    def __init__(self, prefix: str, index: int) -> None:
        self.prefix = prefix
        self.index = index

    def print(self, ps):
        ps.write_string(self.prefix)
        ps.write_string("%d" % self.index)

    def traverse(self, fn):
        fn(self)

    def copy(self, fn, skip):
        if skip(self):
            return None
        return fn(self)


class TypeTemplateParam(AST):
    """TypeTemplateParam is a type template parameter that appears in a
    lambda with explicit template parameters."""

    __slots__ = ("name",)

    def __init__(self, name: AST) -> None:
        self.name = name

    def print(self, ps):
        ps.write_string("typename")
        ps.print_inner(False)
        if not ps.lambda_template_args:
            ps.write_byte(" ")
            ps.print(self.name)

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        name = self.name.copy(fn, skip)
        if name is None:
            return fn(self)
        t = TypeTemplateParam(name)
        r = fn(t)
        return r if r is not None else t


class NonTypeTemplateParam(AST):
    """NonTypeTemplateParam is a non-type template parameter that appears
    in a lambda with explicit template parameters."""

    __slots__ = ("name", "type")

    def __init__(self, name: AST, type: AST) -> None:
        self.name = name
        self.type = type

    def print(self, ps):
        ps.inner.append(self)
        ps.print(self.type)
        if ps.inner:
            if not ps.lambda_template_args:
                ps.write_byte(" ")
                ps.print(self.name)
            ps.inner.pop()

    def print_inner(self, ps):
        ps.print(self.name)

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)
            self.type.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        name = self.name.copy(fn, skip)
        typ = self.type.copy(fn, skip)
        if name is None and typ is None:
            return fn(self)
        if name is None:
            name = self.name
        if typ is None:
            typ = self.type
        t = NonTypeTemplateParam(name, typ)
        r = fn(t)
        return r if r is not None else t


class TemplateTemplateParam(AST):
    """TemplateTemplateParam is a template template parameter that
    appears in a lambda with explicit template parameters."""

    __slots__ = ("name", "params", "constraint")

    def __init__(self, name: AST, params: List[AST], constraint: Optional[AST]) -> None:
        self.name = name
        self.params = params
        self.constraint = constraint

    def print(self, ps):
        lambda_template_args = ps.lambda_template_args
        ps.lambda_template_args = True

        inner = ps.inner
        ps.inner = []

        ps.write_string("template<")
        ps.print_list(self.params, None)
        ps.write_string("> ")
        ps.write_string("class")

        ps.lambda_template_args = lambda_template_args
        ps.inner = inner

        ps.print_inner(False)

        if not ps.lambda_template_args:
            ps.write_byte(" ")
            ps.print(self.name)

        if self.constraint is not None:
            ps.write_string(" requires ")
            ps.print(self.constraint)

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)
            for param in self.params:
                param.traverse(fn)
            if self.constraint is not None:
                self.constraint.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        changed = False
        name = self.name.copy(fn, skip)
        if name is None:
            name = self.name
        else:
            changed = True
        params, ch = _copy_list(self.params, fn, skip)
        changed = changed or ch
        constraint = None
        if self.constraint is not None:
            constraint = self.constraint.copy(fn, skip)
            if constraint is None:
                constraint = self.constraint
            else:
                changed = True
        if not changed:
            return fn(self)
        t = TemplateTemplateParam(name, params, constraint)
        r = fn(t)
        return r if r is not None else t


class ConstrainedTypeTemplateParam(AST):
    """ConstrainedTypeTemplateParam is a constrained template type
    parameter declaration."""

    __slots__ = ("name", "constraint")

    def __init__(self, name: AST, constraint: AST) -> None:
        self.name = name
        self.constraint = constraint

    def print(self, ps):
        ps.inner.append(self)
        ps.print(self.constraint)
        if ps.inner:
            ps.write_byte(" ")
            ps.print(self.name)
            ps.inner.pop()

    def print_inner(self, ps):
        ps.print(self.name)

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)
            self.constraint.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        name = self.name.copy(fn, skip)
        constraint = self.constraint.copy(fn, skip)
        if name is None and constraint is None:
            return fn(self)
        if name is None:
            name = self.name
        if constraint is None:
            constraint = self.constraint
        t = ConstrainedTypeTemplateParam(name, constraint)
        r = fn(t)
        return r if r is not None else t


class TemplateParamPack(AST):
    """TemplateParamPack is a template parameter pack that appears in a
    lambda with explicit template parameters."""

    __slots__ = ("param",)

    def __init__(self, param: AST) -> None:
        self.param = param

    def print(self, ps):
        hold_inner = ps.inner
        ps.inner = [self]

        param = self.param
        is_nttp = isinstance(param, NonTypeTemplateParam)
        if is_nttp:
            ps.print(param.type)
        else:
            ps.print(param)

        if ps.inner:
            ps.write_string("...")
            if is_nttp and not ps.lambda_template_args:
                ps.write_byte(" ")
                ps.print(param.name)

        ps.inner = hold_inner

    def print_inner(self, ps):
        ps.write_string("...")
        if isinstance(self.param, NonTypeTemplateParam):
            ps.print(self.param.name)

    def traverse(self, fn):
        if fn(self):
            self.param.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        param = self.param.copy(fn, skip)
        if param is None:
            return fn(self)
        t = TemplateParamPack(param)
        r = fn(t)
        return r if r is not None else t


class Cast(AST):
    """Cast is a type cast."""

    __slots__ = ("to",)

    def __init__(self, to: AST) -> None:
        self.to = to

    def print(self, ps):
        ps.write_string("operator ")
        ps.print(self.to)

    def traverse(self, fn):
        if fn(self):
            self.to.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        to = self.to.copy(fn, skip)
        if to is None:
            return fn(self)
        t = Cast(to)
        r = fn(t)
        return r if r is not None else t


def parenthesize(ps: PrintState, val: AST) -> None:
    """Print the string for val, wrapped in parentheses if necessary."""
    if isinstance(val, (Name, InitializerList, FunctionParam)):
        paren = False
    elif isinstance(val, Qualified):
        paren = val.local_name
    else:
        paren = True
    if paren:
        ps.start_scope("(")
    ps.print(val)
    if paren:
        ps.end_scope(")")


class Nullary(AST):
    """Nullary is an operator in an expression with no arguments, such as
    throw."""

    __slots__ = ("op",)

    def __init__(self, op: AST) -> None:
        self.op = op

    def print(self, ps):
        if isinstance(self.op, Operator):
            ps.write_string(self.op.name)
        else:
            ps.print(self.op)

    def traverse(self, fn):
        if fn(self):
            self.op.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        op = self.op.copy(fn, skip)
        if op is None:
            return fn(self)
        t = Nullary(op)
        r = fn(t)
        return r if r is not None else t


class Unary(AST):
    """Unary is a unary operation in an expression."""

    __slots__ = ("op", "expr", "suffix", "sizeof_type")

    def __init__(self, op: AST, expr: AST, suffix: bool, sizeof_type: bool) -> None:
        self.op = op
        self.expr = expr
        self.suffix = suffix  # true for ++ -- when used as postfix
        self.sizeof_type = sizeof_type  # true for sizeof (type)

    def print(self, ps):
        op = self.op if isinstance(self.op, Operator) else None
        expr = self.expr

        # Don't print the argument list when taking the address of a
        # function.
        if op is not None and op.name == "&":
            if isinstance(expr, Typed) and isinstance(expr.type, FunctionType):
                expr = expr.name

        if self.suffix:
            parenthesize(ps, expr)

        if op is not None:
            ps.write_string(op.name)
        elif isinstance(self.op, Cast):
            ps.start_scope("(")
            ps.print(self.op.to)
            ps.end_scope(")")
        else:
            ps.print(self.op)

        if not self.suffix:
            always_parens = self.sizeof_type or (
                op is not None and (op.name == "__alignof__" or op.name == "noexcept")
            )
            if op is not None and op.name == "::":
                # Don't use parentheses after ::.
                ps.print(expr)
            elif always_parens:
                ps.start_scope("(")
                ps.print(expr)
                ps.end_scope(")")
            else:
                parenthesize(ps, expr)

    def traverse(self, fn):
        if fn(self):
            self.op.traverse(fn)
            self.expr.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        op = self.op.copy(fn, skip)
        expr = self.expr.copy(fn, skip)
        if op is None and expr is None:
            return fn(self)
        if op is None:
            op = self.op
        if expr is None:
            expr = self.expr
        t = Unary(op, expr, self.suffix, self.sizeof_type)
        r = fn(t)
        return r if r is not None else t


def is_designated_initializer(x: AST) -> bool:
    """Report whether x is a designated initializer."""
    if isinstance(x, Binary):
        op = x.op
        if isinstance(op, Operator):
            if op.name == "]=":
                return True
            if op.name != "=":
                return False
            if isinstance(x.left, Literal):
                return False
            return True
    elif isinstance(x, Trinary):
        op = x.op
        if isinstance(op, Operator):
            return op.name == "[...]="
    return False


class Binary(AST):
    """Binary is a binary operation in an expression."""

    __slots__ = ("op", "left", "right")

    def __init__(self, op: AST, left: AST, right: AST) -> None:
        self.op = op
        self.left = left
        self.right = right

    def print(self, ps):
        op = self.op if isinstance(self.op, Operator) else None

        if op is not None and "cast" in op.name:
            ps.write_string(op.name)
            ps.write_byte("<")
            ps.print(self.left)
            ps.write_string(">")
            ps.start_scope("(")
            ps.print(self.right)
            ps.end_scope(")")
            return

        if is_designated_initializer(self):
            if op.name == "=":
                ps.write_byte(".")
            else:
                ps.write_byte("[")
            ps.print(self.left)
            if op.name == "]=":
                ps.write_byte("]")
            if is_designated_initializer(self.right):
                # Don't add anything between designated
                # initializer chains.
                ps.print(self.right)
            else:
                ps.write_byte("=")
                parenthesize(ps, self.right)
            return

        # Use an extra set of parentheses around an expression that
        # uses the greater-than operator, so that it does not get
        # confused with the '>' that ends template parameters.
        needs_outer_paren = op is not None and (op.name == ">" or op.name == ">>")
        if needs_outer_paren:
            ps.start_scope("(")

        left = self.left

        skip_parens = False

        # For a function call in an expression, don't print the types
        # of the arguments unless there is a return type.
        if op is not None and op.name.startswith("()"):
            ty = self.left
            if isinstance(ty, Typed):
                ft = ty.type
                if isinstance(ft, FunctionType):
                    if ft.ret is None:
                        left = ty.name
                    else:
                        if op.name != "() ":
                            skip_parens = True
                else:
                    left = ty.name

        if skip_parens:
            ps.print(left)
        else:
            parenthesize(ps, left)

        if op is not None and op.name == "[]":
            ps.write_byte("[")
            ps.print(self.right)
            ps.write_byte("]")
            return

        if op is not None:
            if not op.name.startswith("()"):
                ps.write_string(op.name)
        else:
            ps.print(self.op)

        parenthesize(ps, self.right)

        if needs_outer_paren:
            ps.end_scope(")")

    def traverse(self, fn):
        if fn(self):
            self.op.traverse(fn)
            self.left.traverse(fn)
            self.right.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        op = self.op.copy(fn, skip)
        left = self.left.copy(fn, skip)
        right = self.right.copy(fn, skip)
        if op is None and left is None and right is None:
            return fn(self)
        if op is None:
            op = self.op
        if left is None:
            left = self.left
        if right is None:
            right = self.right
        t = Binary(op, left, right)
        r = fn(t)
        return r if r is not None else t


class Trinary(AST):
    """Trinary is the ?: trinary operation in an expression."""

    __slots__ = ("op", "first", "second", "third")

    def __init__(self, op: AST, first: AST, second: AST, third: AST) -> None:
        self.op = op
        self.first = first
        self.second = second
        self.third = third

    def print(self, ps):
        if is_designated_initializer(self):
            ps.write_byte("[")
            ps.print(self.first)
            ps.write_string(" ... ")
            ps.print(self.second)
            ps.write_byte("]")
            if is_designated_initializer(self.third):
                # Don't add anything between designated
                # initializer chains.
                ps.print(self.third)
            else:
                ps.write_byte("=")
                parenthesize(ps, self.third)
            return

        parenthesize(ps, self.first)
        ps.write_byte("?")
        parenthesize(ps, self.second)
        ps.write_string(" : ")
        parenthesize(ps, self.third)

    def traverse(self, fn):
        if fn(self):
            self.op.traverse(fn)
            self.first.traverse(fn)
            self.second.traverse(fn)
            self.third.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        op = self.op.copy(fn, skip)
        first = self.first.copy(fn, skip)
        second = self.second.copy(fn, skip)
        third = self.third.copy(fn, skip)
        if op is None and first is None and second is None and third is None:
            return fn(self)
        if op is None:
            op = self.op
        if first is None:
            first = self.first
        if second is None:
            second = self.second
        if third is None:
            third = self.third
        t = Trinary(op, first, second, third)
        r = fn(t)
        return r if r is not None else t


class Fold(AST):
    """Fold is a C++17 fold-expression.  Arg2 is None for a unary
    operator."""

    __slots__ = ("left", "op", "arg1", "arg2")

    def __init__(self, left: bool, op: AST, arg1: AST, arg2: Optional[AST]) -> None:
        self.left = left
        self.op = op
        self.arg1 = arg1
        self.arg2 = arg2

    def print(self, ps):
        op = self.op if isinstance(self.op, Operator) else None

        def print_op():
            if op is not None:
                ps.write_string(op.name)
            else:
                ps.print(self.op)

        if self.arg2 is None:
            if self.left:
                ps.start_scope("(")
                ps.write_string("...")
                print_op()
                parenthesize(ps, self.arg1)
                ps.end_scope(")")
            else:
                ps.start_scope("(")
                parenthesize(ps, self.arg1)
                print_op()
                ps.write_string("...")
                ps.end_scope(")")
        else:
            ps.start_scope("(")
            parenthesize(ps, self.arg1)
            print_op()
            ps.write_string("...")
            print_op()
            parenthesize(ps, self.arg2)
            ps.end_scope(")")

    def traverse(self, fn):
        if fn(self):
            self.op.traverse(fn)
            self.arg1.traverse(fn)
            if self.arg2 is not None:
                self.arg2.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        op = self.op.copy(fn, skip)
        arg1 = self.arg1.copy(fn, skip)
        arg2 = None
        if self.arg2 is not None:
            arg2 = self.arg2.copy(fn, skip)
        if op is None and arg1 is None and arg2 is None:
            return fn(self)
        if op is None:
            op = self.op
        if arg1 is None:
            arg1 = self.arg1
        if arg2 is None:
            arg2 = self.arg2
        t = Fold(self.left, op, arg1, arg2)
        r = fn(t)
        return r if r is not None else t


class Subobject(AST):
    """Subobject is a a reference to an offset in an expression.  This is
    used for C++20 manglings of class types used as the type of
    non-type template arguments.

    See https://github.com/itanium-cxx-abi/cxx-abi/issues/47."""

    __slots__ = ("type", "sub_expr", "offset", "selectors", "past_end")

    def __init__(self, type: AST, sub_expr: AST, offset: int, selectors: List[int], past_end: bool) -> None:
        self.type = type
        self.sub_expr = sub_expr
        self.offset = offset
        self.selectors = selectors
        self.past_end = past_end

    def print(self, ps):
        ps.print(self.sub_expr)
        ps.write_string(".<")
        ps.print(self.type)
        ps.write_string(" at offset %d>" % self.offset)

    def traverse(self, fn):
        if fn(self):
            self.type.traverse(fn)
            self.sub_expr.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        typ = self.type.copy(fn, skip)
        sub_expr = self.sub_expr.copy(fn, skip)
        if typ is None and sub_expr is None:
            # Upstream returns nil here without calling fn.
            return None
        if typ is None:
            typ = self.type
        if sub_expr is None:
            sub_expr = self.sub_expr
        t = Subobject(typ, sub_expr, self.offset, self.selectors, self.past_end)
        r = fn(t)
        return r if r is not None else t


class PtrMemCast(AST):
    """PtrMemCast is a conversion of an expression to a pointer-to-member
    type.  This is used for C++20 manglings of class types used as the
    type of non-type template arguments.

    See https://github.com/itanium-cxx-abi/cxx-abi/issues/47."""

    __slots__ = ("type", "expr", "offset")

    def __init__(self, type: AST, expr: AST, offset: int) -> None:
        self.type = type
        self.expr = expr
        self.offset = offset

    def print(self, ps):
        ps.start_scope("(")
        ps.print(self.type)
        ps.write_string(")(")
        ps.print(self.expr)
        ps.end_scope(")")

    def traverse(self, fn):
        if fn(self):
            self.type.traverse(fn)
            self.expr.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        typ = self.type.copy(fn, skip)
        expr = self.expr.copy(fn, skip)
        if typ is None and expr is None:
            # Upstream returns nil here without calling fn.
            return None
        if typ is None:
            typ = self.type
        if expr is None:
            expr = self.expr
        t = PtrMemCast(typ, expr, self.offset)
        r = fn(t)
        return r if r is not None else t


class New(AST):
    """New is a use of operator new in an expression."""

    __slots__ = ("op", "place", "type", "init")

    def __init__(self, op: AST, place: Optional[AST], type: AST, init: Optional[AST]) -> None:
        self.op = op
        self.place = place
        self.type = type
        self.init = init

    def print(self, ps):
        # Op doesn't really matter for printing--we always print "new".
        ps.write_string("new ")
        if self.place is not None:
            parenthesize(ps, self.place)
            ps.write_byte(" ")
        ps.print(self.type)
        if self.init is not None:
            parenthesize(ps, self.init)

    def traverse(self, fn):
        if fn(self):
            self.op.traverse(fn)
            if self.place is not None:
                self.place.traverse(fn)
            self.type.traverse(fn)
            if self.init is not None:
                self.init.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        op = self.op.copy(fn, skip)
        place = None
        if self.place is not None:
            place = self.place.copy(fn, skip)
        typ = self.type.copy(fn, skip)
        ini = None
        if self.init is not None:
            ini = self.init.copy(fn, skip)
        if op is None and place is None and typ is None and ini is None:
            return fn(self)
        if op is None:
            op = self.op
        if place is None:
            place = self.place
        if typ is None:
            typ = self.type
        if ini is None:
            ini = self.init
        t = New(op, place, typ, ini)
        r = fn(t)
        return r if r is not None else t


# Suffixes to use for constants of the given integer type.
BUILTIN_TYPE_SUFFIX = {
    "int": "",
    "unsigned int": "u",
    "long": "l",
    "unsigned long": "ul",
    "long long": "ll",
    "unsigned long long": "ull",
}

# Builtin float types.
BUILTIN_TYPE_FLOAT = {
    "double",
    "long double",
    "float",
    "__float128",
    "half",
}


class Literal(AST):
    """Literal is a literal in an expression."""

    __slots__ = ("type", "val", "neg")

    def __init__(self, type: AST, val: str, neg: bool) -> None:
        self.type = type
        self.val = val
        self.neg = neg

    def print(self, ps):
        is_float = False
        b = self.type
        if isinstance(b, BuiltinType):
            suffix = BUILTIN_TYPE_SUFFIX.get(b.name)
            if suffix is not None:
                if self.neg:
                    ps.write_byte("-")
                ps.write_string(self.val)
                ps.write_string(suffix)
                return
            elif b.name == "bool" and not self.neg:
                if self.val == "0":
                    ps.write_string("false")
                    return
                elif self.val == "1":
                    ps.write_string("true")
                    return
            elif b.name == "decltype(nullptr)" and (self.val == "" or self.val == "0"):
                ps.print(self.type)
                return
            else:
                is_float = b.name in BUILTIN_TYPE_FLOAT

        ps.start_scope("(")
        ps.print(self.type)
        ps.end_scope(")")

        if is_float:
            ps.write_byte("[")
        if self.neg:
            ps.write_byte("-")
        ps.write_string(self.val)
        if is_float:
            ps.write_byte("]")

    def traverse(self, fn):
        if fn(self):
            self.type.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        typ = self.type.copy(fn, skip)
        if typ is None:
            return fn(self)
        t = Literal(typ, self.val, self.neg)
        r = fn(t)
        return r if r is not None else t


class StringLiteral(AST):
    """StringLiteral is a string literal."""

    __slots__ = ("type",)

    def __init__(self, type: AST) -> None:
        self.type = type

    def print(self, ps):
        ps.write_string('"<')
        self.type.print(ps)
        ps.write_string('>"')

    def traverse(self, fn):
        if fn(self):
            self.type.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        typ = self.type.copy(fn, skip)
        if typ is None:
            return fn(self)
        t = StringLiteral(typ)
        r = fn(t)
        return r if r is not None else t


class LambdaExpr(AST):
    """LambdaExpr is a literal that is a lambda expression."""

    __slots__ = ("type",)

    def __init__(self, type: AST) -> None:
        self.type = type

    def print(self, ps):
        ps.write_string("[]")
        if isinstance(self.type, Closure):
            self.type.print_types(ps)
        ps.write_string("{...}")

    def traverse(self, fn):
        if fn(self):
            self.type.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        typ = self.type.copy(fn, skip)
        if typ is None:
            return fn(self)
        t = LambdaExpr(typ)
        r = fn(t)
        return r if r is not None else t


class ExprList(AST):
    """ExprList is a list of expressions, typically arguments to a
    function call in an expression."""

    __slots__ = ("exprs",)

    def __init__(self, exprs: List[AST]) -> None:
        self.exprs = exprs

    def print(self, ps):
        ps.print_list(self.exprs, None)

    def traverse(self, fn):
        if fn(self):
            for e in self.exprs:
                e.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        exprs, changed = _copy_list(self.exprs, fn, skip)
        if not changed:
            return fn(self)
        t = ExprList(exprs)
        r = fn(t)
        return r if r is not None else t


def _go_atoi(s: str) -> Optional[int]:
    """strconv.Atoi for a 64-bit int: optional sign, decimal digits,
    nothing else.  Returns None on error."""
    t = s
    if t[:1] in ("+", "-"):
        t = t[1:]
    if not t:
        return None
    for c in t:
        if not ("0" <= c <= "9"):
            return None
    v = int(s)
    if v > 0x7FFFFFFFFFFFFFFF or v < -0x8000000000000000:
        return None
    return v


_C_ESCAPES = {
    7: "\\a",
    8: "\\b",
    12: "\\f",
    10: "\\n",
    13: "\\r",
    9: "\\t",
    11: "\\v",
    0x22: '\\"',
    0x5C: "\\\\",
}


class InitializerList(AST):
    """InitializerList is an initializer list: an optional type with a
    list of expressions."""

    __slots__ = ("type", "exprs")

    def __init__(self, type: Optional[AST], exprs: AST) -> None:
        self.type = type
        self.exprs = exprs

    def print(self, ps):
        if self.type is not None:
            array = self.type
            if isinstance(array, ArrayType):
                builtin = array.element
                if isinstance(builtin, BuiltinType) and builtin.name == "char":
                    if self.print_as_string(ps):
                        return
        if self.type is not None:
            ps.print(self.type)
        ps.write_byte("{")
        ps.print(self.exprs)
        ps.write_byte("}")

    def print_as_string(self, ps) -> bool:
        """Try to print a char array initializer as a string.  Report
        whether it succeeded."""
        expr_list = self.exprs
        if not isinstance(expr_list, ExprList):
            return False

        sb = ['"']
        in_numeric_escape = False
        for e in expr_list.exprs:
            if not isinstance(e, Literal):
                return False
            if e.neg:
                return False
            val = _go_atoi(e.val)
            if val is None or val > 255:
                return False

            # If we just added a numeric escape,
            # make sure it doesn't get accidentally extended
            # (numeric escapes here are C++ syntax, not Go syntax).
            if in_numeric_escape:
                if (0x30 <= val <= 0x39) or (0x61 <= val <= 0x66) or (0x41 <= val <= 0x46):
                    sb.append('""')

            in_numeric_escape = False
            esc = _C_ESCAPES.get(val)
            if esc is not None:
                sb.append(esc)
            elif val < 32 or val == 127:
                hexd = "0123456789ABCDEF"
                sb.append("\\")
                if val > 7:
                    sb.append("x")
                if val >= 16:
                    sb.append(hexd[val >> 4])
                sb.append(hexd[val & 0xF])
                in_numeric_escape = True
            else:
                sb.append(chr(val))

        sb.append('"')
        ps.write_string("".join(sb))
        return True

    def traverse(self, fn):
        if fn(self):
            if self.type is not None:
                self.type.traverse(fn)
            self.exprs.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        typ = None
        if self.type is not None:
            typ = self.type.copy(fn, skip)
        exprs = self.exprs.copy(fn, skip)
        if typ is None and exprs is None:
            return fn(self)
        if typ is None:
            typ = self.type
        if exprs is None:
            exprs = self.exprs
        t = InitializerList(typ, exprs)
        r = fn(t)
        return r if r is not None else t


class DefaultArg(AST):
    """DefaultArg holds a default argument for a local name."""

    __slots__ = ("num", "arg")

    def __init__(self, num: int, arg: AST) -> None:
        self.num = num
        self.arg = arg

    def print(self, ps):
        ps.write_raw("{default arg#%d}::" % (self.num + 1))
        ps.print(self.arg)

    def traverse(self, fn):
        if fn(self):
            self.arg.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        arg = self.arg.copy(fn, skip)
        if arg is None:
            return fn(self)
        t = DefaultArg(self.num, arg)
        r = fn(t)
        return r if r is not None else t


class Closure(AST):
    """Closure is a closure, or lambda expression."""

    __slots__ = ("template_args", "template_args_constraint", "types", "num", "call_constraint")

    def __init__(
        self,
        template_args: List[AST],
        template_args_constraint: Optional[AST],
        types: List[AST],
        num: int,
        call_constraint: Optional[AST],
    ) -> None:
        self.template_args = template_args
        self.template_args_constraint = template_args_constraint
        self.types = types
        self.num = num
        self.call_constraint = call_constraint

    def print(self, ps):
        ps.write_string("{lambda")
        self.print_types(ps)
        ps.write_string("#%d}" % (self.num + 1))

    def print_types(self, ps):
        if self.template_args:
            ps.write_string("<")
            ps.print_list(self.template_args, None)
            ps.write_string(">")

        if self.template_args_constraint is not None:
            ps.write_string(" requires ")
            ps.print(self.template_args_constraint)
            ps.write_byte(" ")

        ps.start_scope("(")
        ps.print_list(self.types, None)
        ps.end_scope(")")

        if self.call_constraint is not None:
            ps.write_string(" requires ")
            ps.print(self.call_constraint)

    def traverse(self, fn):
        if fn(self):
            for a in self.template_args:
                a.traverse(fn)
            if self.template_args_constraint is not None:
                self.template_args_constraint.traverse(fn)
            for t in self.types:
                t.traverse(fn)
            if self.call_constraint is not None:
                self.call_constraint.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        args, changed = _copy_list(self.template_args, fn, skip)

        template_args_constraint = None
        if self.template_args_constraint is not None:
            template_args_constraint = self.template_args_constraint.copy(fn, skip)
            if template_args_constraint is None:
                template_args_constraint = self.template_args_constraint
            else:
                changed = True

        types, ch = _copy_list(self.types, fn, skip)
        changed = changed or ch

        call_constraint = None
        if self.call_constraint is not None:
            call_constraint = self.call_constraint.copy(fn, skip)
            if call_constraint is None:
                call_constraint = self.call_constraint
            else:
                changed = True

        if not changed:
            return fn(self)
        t = Closure(args, template_args_constraint, types, self.num, call_constraint)
        r = fn(t)
        return r if r is not None else t


class StructuredBindings(AST):
    """StructuredBindings is a structured binding declaration."""

    __slots__ = ("bindings",)

    def __init__(self, bindings: List[AST]) -> None:
        self.bindings = bindings

    def print(self, ps):
        ps.write_string("[")
        ps.print_list(self.bindings, None)
        ps.write_string("]")

    def traverse(self, fn):
        if fn(self):
            for b in self.bindings:
                b.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        bindings, changed = _copy_list(self.bindings, fn, skip)
        if not changed:
            return fn(self)
        t = StructuredBindings(bindings)
        r = fn(t)
        return r if r is not None else t


class UnnamedType(AST):
    """UnnamedType is an unnamed type, that just has an index."""

    __slots__ = ("num",)

    def __init__(self, num: int) -> None:
        self.num = num

    def print(self, ps):
        ps.write_string("{unnamed type#%d}" % (self.num + 1))

    def traverse(self, fn):
        fn(self)

    def copy(self, fn, skip):
        if skip(self):
            return None
        return fn(self)


class Clone(AST):
    """Clone is a clone of a function, with a distinguishing suffix."""

    __slots__ = ("base", "suffix")

    def __init__(self, base: AST, suffix: str) -> None:
        self.base = base
        self.suffix = suffix

    def print(self, ps):
        ps.print(self.base)
        ps.write_string(" [clone %s]" % self.suffix)

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = Clone(base, self.suffix)
        r = fn(t)
        return r if r is not None else t


class Special(AST):
    """Special is a special symbol, printed as a prefix plus another
    value."""

    __slots__ = ("prefix", "val")

    def __init__(self, prefix: str, val: AST) -> None:
        self.prefix = prefix
        self.val = val

    def print(self, ps):
        ps.write_string(self.prefix)
        ps.print(self.val)

    def traverse(self, fn):
        if fn(self):
            self.val.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        val = self.val.copy(fn, skip)
        if val is None:
            return fn(self)
        t = Special(self.prefix, val)
        r = fn(t)
        return r if r is not None else t


class Special2(AST):
    """Special2 is like special, but uses two values."""

    __slots__ = ("prefix", "val1", "middle", "val2")

    def __init__(self, prefix: str, val1: AST, middle: str, val2: AST) -> None:
        self.prefix = prefix
        self.val1 = val1
        self.middle = middle
        self.val2 = val2

    def print(self, ps):
        ps.write_string(self.prefix)
        ps.print(self.val1)
        ps.write_string(self.middle)
        ps.print(self.val2)

    def traverse(self, fn):
        if fn(self):
            self.val1.traverse(fn)
            self.val2.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        val1 = self.val1.copy(fn, skip)
        val2 = self.val2.copy(fn, skip)
        if val1 is None and val2 is None:
            return fn(self)
        if val1 is None:
            val1 = self.val1
        if val2 is None:
            val2 = self.val2
        t = Special2(self.prefix, val1, self.middle, val2)
        r = fn(t)
        return r if r is not None else t


class EnableIf(AST):
    """EnableIf is used by clang for an enable_if attribute."""

    __slots__ = ("type", "args")

    def __init__(self, type: AST, args: List[AST]) -> None:
        self.type = type
        self.args = args

    def print(self, ps):
        ps.print(self.type)
        ps.write_string(" [enable_if:")
        ps.print_list(self.args, None)
        ps.write_string("]")

    def traverse(self, fn):
        if fn(self):
            self.type.traverse(fn)
            for a in self.args:
                a.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        typ = self.type.copy(fn, skip)
        args, args_changed = _copy_list(self.args, fn, skip)
        if typ is None and not args_changed:
            return fn(self)
        if typ is None:
            typ = self.type
        t = EnableIf(typ, args)
        r = fn(t)
        return r if r is not None else t


class ModuleName(AST):
    """ModuleName is a C++20 module."""

    __slots__ = ("parent", "name", "is_partition")

    def __init__(self, parent: Optional[AST], name: AST, is_partition: bool) -> None:
        self.parent = parent
        self.name = name
        self.is_partition = is_partition

    def print(self, ps):
        if self.parent is not None:
            ps.print(self.parent)
        if self.is_partition:
            ps.write_byte(":")
        elif self.parent is not None:
            ps.write_byte(".")
        ps.print(self.name)

    def traverse(self, fn):
        if fn(self):
            # Like upstream, this does not check for a nil Parent; a
            # nil Parent panics in Go (AttributeError here).
            self.parent.traverse(fn)
            self.name.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        parent = None
        if self.parent is not None:
            parent = self.parent.copy(fn, skip)
        name = self.name.copy(fn, skip)
        if parent is None and name is None:
            return fn(self)
        if parent is None:
            parent = self.parent
        if name is None:
            name = self.name
        t = ModuleName(parent, name, self.is_partition)
        r = fn(t)
        return r if r is not None else t


class ModuleEntity(AST):
    """ModuleEntity is a name inside a module."""

    __slots__ = ("module", "name")

    def __init__(self, module: AST, name: AST) -> None:
        self.module = module
        self.name = name

    def print(self, ps):
        ps.print(self.name)
        ps.write_byte("@")
        ps.print(self.module)

    def traverse(self, fn):
        if fn(self):
            self.module.traverse(fn)
            self.name.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        module = self.module.copy(fn, skip)
        name = self.name.copy(fn, skip)
        if module is None and name is None:
            return fn(self)
        if module is None:
            module = self.module
        if name is None:
            name = self.name
        t = ModuleEntity(module, name)
        r = fn(t)
        return r if r is not None else t


class Friend(AST):
    """Friend is a member like friend name."""

    __slots__ = ("name",)

    def __init__(self, name: AST) -> None:
        self.name = name

    def print(self, ps):
        ps.print(self.name)
        ps.write_string("[friend]")

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        name = self.name.copy(fn, skip)
        if name is None:
            return fn(self)
        t = Friend(name)
        r = fn(t)
        return r if r is not None else t


class Constraint(AST):
    """Constraint represents an AST with a constraint.
    ForTemplateArgs is true if this constraint was found at the
    end of a list of template arguments (only relevant for LLVM
    style)."""

    __slots__ = ("name", "requires", "for_template_args")

    def __init__(self, name: AST, requires: AST, for_template_args: bool) -> None:
        self.name = name
        self.requires = requires
        self.for_template_args = for_template_args

    def print(self, ps):
        ps.print(self.name)
        ps.write_string(" requires ")
        ps.print(self.requires)

    def traverse(self, fn):
        if fn(self):
            self.name.traverse(fn)
            self.requires.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        name = self.name.copy(fn, skip)
        requires = self.requires.copy(fn, skip)
        if name is None and requires is None:
            return fn(self)
        if name is None:
            name = self.name
        if requires is None:
            requires = self.requires
        t = Constraint(name, requires, self.for_template_args)
        r = fn(t)
        return r if r is not None else t


class RequiresExpr(AST):
    """RequiresExpr is a C++20 requires expression."""

    __slots__ = ("params", "requirements")

    def __init__(self, params: List[AST], requirements: List[AST]) -> None:
        self.params = params
        self.requirements = requirements

    def print(self, ps):
        ps.write_string("requires")
        if self.params:
            ps.write_byte(" ")
            ps.start_scope("(")
            ps.print_list(self.params, None)
            ps.end_scope(")")
        ps.write_byte(" ")
        ps.start_scope("{")
        for req in self.requirements:
            ps.print(req)
        ps.write_byte(" ")
        ps.end_scope("}")

    def traverse(self, fn):
        if fn(self):
            for p in self.params:
                p.traverse(fn)
            for r in self.requirements:
                r.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        params, changed = _copy_list(self.params, fn, skip)
        requirements, ch = _copy_list(self.requirements, fn, skip)
        if not (changed or ch):
            return fn(self)
        t = RequiresExpr(params, requirements)
        r = fn(t)
        return r if r is not None else t


class ExprRequirement(AST):
    """ExprRequirement is a simple requirement in a requires expression.
    This is an arbitrary expression."""

    __slots__ = ("expr", "noexcept", "type_req")

    def __init__(self, expr: AST, noexcept: bool, type_req: Optional[AST]) -> None:
        self.expr = expr
        self.noexcept = noexcept
        self.type_req = type_req

    def print(self, ps):
        ps.write_byte(" ")
        if self.noexcept or self.type_req is not None:
            ps.start_scope("{")
        ps.print(self.expr)
        if self.noexcept or self.type_req is not None:
            ps.end_scope("}")
        if self.noexcept:
            ps.write_string(" noexcept")
        if self.type_req is not None:
            ps.write_string(" -> ")
            ps.print(self.type_req)
        ps.write_byte(";")

    def traverse(self, fn):
        if fn(self):
            self.expr.traverse(fn)
            if self.type_req is not None:
                self.type_req.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        expr = self.expr.copy(fn, skip)
        type_req = None
        if self.type_req is not None:
            type_req = self.type_req.copy(fn, skip)
        if expr is None and type_req is None:
            return fn(self)
        if expr is None:
            expr = self.expr
        if type_req is None:
            type_req = self.type_req
        # Upstream drops the Noexcept flag in the copy.
        t = ExprRequirement(expr, False, type_req)
        r = fn(t)
        return r if r is not None else t


class TypeRequirement(AST):
    """TypeRequirement is a type requirement in a requires expression."""

    __slots__ = ("type",)

    def __init__(self, type: AST) -> None:
        self.type = type

    def print(self, ps):
        ps.write_string(" typename ")
        ps.print(self.type)
        ps.write_byte(";")

    def traverse(self, fn):
        if fn(self):
            self.type.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        typ = self.type.copy(fn, skip)
        if typ is None:
            return fn(self)
        t = TypeRequirement(typ)
        r = fn(t)
        return r if r is not None else t


class NestedRequirement(AST):
    """NestedRequirement is a nested requirement in a requires
    expression."""

    __slots__ = ("constraint",)

    def __init__(self, constraint: AST) -> None:
        self.constraint = constraint

    def print(self, ps):
        ps.write_string(" requires ")
        ps.print(self.constraint)
        ps.write_byte(";")

    def traverse(self, fn):
        if fn(self):
            self.constraint.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        constraint = self.constraint.copy(fn, skip)
        if constraint is None:
            return fn(self)
        t = NestedRequirement(constraint)
        r = fn(t)
        return r if r is not None else t


class ExplicitObjectParameter(AST):
    """ExplicitObjectParameter represents a C++23 explicit object
    parameter."""

    __slots__ = ("base",)

    def __init__(self, base: AST) -> None:
        self.base = base

    def print(self, ps):
        ps.write_string("this ")
        ps.print(self.base)

    def traverse(self, fn):
        if fn(self):
            self.base.traverse(fn)

    def copy(self, fn, skip):
        if skip(self):
            return None
        base = self.base.copy(fn, skip)
        if base is None:
            return fn(self)
        t = ExplicitObjectParameter(base)
        r = fn(t)
        return r if r is not None else t
