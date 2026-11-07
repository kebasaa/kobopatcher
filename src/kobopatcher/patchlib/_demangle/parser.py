# Port of github.com/ianlancetaylor/demangle at version
# v0.0.0-20250628045327-2d64ad6b7ec5 (BSD-3-Clause, see LICENSE in this
# directory).  This module corresponds to demangle.go.
#
# Only the behaviour of ToString/ToAST with *default options* is
# ported: NoParams, NoClones and Verbose (the only options that affect
# parsing) are not supported, so the code paths that only run with
# them are omitted.  Everything else follows demangle.go function by
# function.
#
# Representation: instead of Go's shrinking st.str slice, the state
# keeps the string being demangled in st.s plus a position st.pos, so
# Go's st.off is st.pos.  st.s carries one extra "\0" sentinel after the
# real string (st.n is the real length), which lets the frequent Go
# test `len(st.str) > 0 && st.str[0] == c` be written `s[pos] == c`
# (no comparison in the parser is against "\0").  Strings hold one byte
# per character (see __init__.py).
"""Parser for Itanium C++ ABI mangled names."""

from __future__ import annotations

from typing import List, Optional, Tuple

from .ast import (
    AST,
    ArgumentPack,
    ArrayType,
    Binary,
    BinaryFP,
    BitIntType,
    BuiltinType,
    Cast,
    Clone,
    Closure,
    ComplexType,
    ConstrainedTypeTemplateParam,
    Constraint,
    Constructor,
    Decltype,
    DefaultArg,
    Destructor,
    ElaboratedType,
    EnableIf,
    ExplicitObjectParameter,
    ExprList,
    ExprRequirement,
    FixedType,
    Fold,
    Friend,
    FunctionParam,
    FunctionType,
    GlobalCDtor,
    ImaginaryType,
    InitializerList,
    LambdaAuto,
    LambdaExpr,
    Literal,
    MethodWithQualifiers,
    ModuleEntity,
    ModuleName,
    Name,
    NestedRequirement,
    New,
    NonTypeTemplateParam,
    Nullary,
    Operator,
    PackExpansion,
    PointerType,
    PtrMem,
    PtrMemCast,
    Qualified,
    Qualifier,
    Qualifiers,
    ReferenceType,
    RequiresExpr,
    RvalueReferenceType,
    SizeofArgs,
    SizeofPack,
    Special,
    Special2,
    StringLiteral,
    StructuredBindings,
    Subobject,
    SuffixType,
    TaggedName,
    Template,
    TemplateParam,
    TemplateParamName,
    TemplateParamPack,
    TemplateParamQualifiedArg,
    TemplateTemplateParam,
    TransformedType,
    Trinary,
    TypeRequirement,
    TypeTemplateParam,
    TypeWithQualifiers,
    Typed,
    Unary,
    UnnamedType,
    VectorType,
    VendorQualifier,
)
from .errors import DemangleError, InternalError, NotMangledNameError


def is_digit(c: str) -> bool:
    """Whether c is a digit for demangling purposes."""
    return "0" <= c <= "9"


def is_upper(c: str) -> bool:
    """Whether c is an upper case letter for demangling purposes."""
    return "A" <= c <= "Z"


def is_lower(c: str) -> bool:
    """Whether c is a lower case letter for demangling purposes."""
    return "a" <= c <= "z"


def _adjust(e: DemangleError, adj: int) -> DemangleError:
    """adjustErr: adjust the offset of a demangleErr."""
    if e.off is not None and type(e) is DemangleError:
        return DemangleError(e.err, e.off + adj)
    return e


def to_ast(name: str) -> AST:
    """ToAST with default options."""
    if name.startswith("_Z"):
        try:
            return do_demangle(name[2:])
        except DemangleError as e:
            raise _adjust(e, 2) from None

    if name.startswith("___Z"):
        # clang extensions
        block = name.rfind("_block_invoke")
        if block == -1:
            raise NotMangledNameError()
        try:
            a = do_demangle(name[4:block])
        except DemangleError as e:
            raise _adjust(e, 4) from None
        name = name[block:][len("_block_invoke"):]
        if name[:1] == "_":
            name = name[1:]
        while name and is_digit(name[0]):
            name = name[1:]
        if name and name[0] != ".":
            raise DemangleError("unparsed characters at end of mangled name")
        return Special("invocation function for block in ", a)

    prefix = "_GLOBAL_"
    if name.startswith(prefix):
        try:
            return global_cdtor_name(name[len(prefix):])
        except DemangleError as e:
            raise _adjust(e, len(prefix)) from None

    raise NotMangledNameError()


def global_cdtor_name(name: str) -> AST:
    """Demangle a global constructor/destructor symbol name.  The
    parameter is the string following the "_GLOBAL_" prefix."""
    if len(name) < 4:
        raise NotMangledNameError()
    if name[0] not in ".$_":
        raise NotMangledNameError()

    c = name[1]
    if c == "I":
        ctor = True
    elif c == "D":
        ctor = False
    else:
        raise NotMangledNameError()

    if name[2] != "_":
        raise NotMangledNameError()

    if not name.startswith("_Z", 3):
        return GlobalCDtor(ctor, Name(name))
    try:
        a = do_demangle(name[5:])
    except DemangleError as e:
        raise _adjust(e, 5) from None
    return GlobalCDtor(ctor, a)


def do_demangle(name: str) -> AST:
    """The entry point into the demangler proper."""
    st = State(name)
    a = st.encoding(NOT_FOR_LOCAL_NAME)

    # Accept a clone suffix.
    s = st.s
    while st.n - st.pos > 1 and s[st.pos] == "." and (
        is_lower(s[st.pos + 1]) or s[st.pos + 1] == "_" or is_digit(s[st.pos + 1])
    ):
        a = st.clone_suffix(a)

    if st.pos < st.n:
        st.fail("unparsed characters at end of mangled name")

    return a


FOR_LOCAL_NAME = 0
NOT_FOR_LOCAL_NAME = 1


# An operator is the demangled name, and the number of arguments it
# takes in an expression.  (The Go table also holds the precedence,
# which is only used for LLVM style output.)
OPERATORS = {
    "aN": ("&=", 2),
    "aS": ("=", 2),
    "aa": ("&&", 2),
    "ad": ("&", 1),
    "an": ("&", 2),
    "at": ("alignof ", 1),
    "aw": ("co_await ", 1),
    "az": ("alignof ", 1),
    "cc": ("const_cast", 2),
    "cl": ("()", 2),
    # cp is not in the ABI but is used by clang "when the call
    # would use ADL except for being parenthesized."
    # The trailing space indicates this to the printer.
    "cp": ("() ", 2),
    "cm": (",", 2),
    "co": ("~", 1),
    "dV": ("/=", 2),
    "dX": ("[...]=", 3),
    "da": ("delete[] ", 1),
    "dc": ("dynamic_cast", 2),
    "de": ("*", 1),
    "di": ("=", 2),
    "dl": ("delete ", 1),
    "ds": (".*", 2),
    "dt": (".", 2),
    "dv": ("/", 2),
    "dx": ("]=", 2),
    "eO": ("^=", 2),
    "eo": ("^", 2),
    "eq": ("==", 2),
    "fl": ("...", 2),
    "fr": ("...", 2),
    "fL": ("...", 3),
    "fR": ("...", 3),
    "ge": (">=", 2),
    "gs": ("::", 1),
    "gt": (">", 2),
    "ix": ("[]", 2),
    "lS": ("<<=", 2),
    "le": ("<=", 2),
    "li": ('operator"" ', 1),
    "ls": ("<<", 2),
    "lt": ("<", 2),
    "mI": ("-=", 2),
    "mL": ("*=", 2),
    "mi": ("-", 2),
    "ml": ("*", 2),
    "mm": ("--", 1),
    "na": ("new[]", 3),
    "ne": ("!=", 2),
    "ng": ("-", 1),
    "nt": ("!", 1),
    "nw": ("new", 3),
    "nx": ("noexcept", 1),
    "oR": ("|=", 2),
    "oo": ("||", 2),
    "or": ("|", 2),
    "pL": ("+=", 2),
    "pl": ("+", 2),
    "pm": ("->*", 2),
    "pp": ("++", 1),
    "ps": ("+", 1),
    "pt": ("->", 2),
    "qu": ("?", 3),
    "rM": ("%=", 2),
    "rS": (">>=", 2),
    "rc": ("reinterpret_cast", 2),
    "rm": ("%", 2),
    "rs": (">>", 2),
    "sP": ("sizeof...", 1),
    "sZ": ("sizeof...", 1),
    "sc": ("static_cast", 2),
    "ss": ("<=>", 2),
    "st": ("sizeof ", 1),
    "sz": ("sizeof ", 1),
    "te": ("typeid ", 1),
    "ti": ("typeid ", 1),
    "tr": ("throw", 0),
    "tw": ("throw ", 1),
}

# builtinTypes maps the type letter to the type name.
BUILTIN_TYPES = {
    "a": "signed char",
    "b": "bool",
    "c": "char",
    "d": "double",
    "e": "long double",
    "f": "float",
    "g": "__float128",
    "h": "unsigned char",
    "i": "int",
    "j": "unsigned int",
    "l": "long",
    "m": "unsigned long",
    "n": "__int128",
    "o": "unsigned __int128",
    "s": "short",
    "t": "unsigned short",
    "v": "void",
    "w": "wchar_t",
    "x": "long long",
    "y": "unsigned long long",
    "z": "...",
}

# qualifiers maps from the character used in the mangled name to the
# string to print.
QUALIFIERS = {
    "r": "restrict",
    "V": "volatile",
    "K": "const",
}


def _fixed_prefix(c: str) -> Optional[str]:
    if c == "s":
        return "short "
    if c == "t":
        return "unsigned short "
    if c == "i":
        return ""
    if c == "j":
        return "unsigned "
    if c == "l":
        return "long "
    if c == "m":
        return "unsigned long "
    return None


def _std(n: str) -> Qualified:
    return Qualified(Name("std"), Name(n), False)


# subAST maps standard substitution codes to the corresponding AST.
SUB_AST = {
    "t": Name("std"),
    "a": _std("allocator"),
    "b": _std("basic_string"),
    "s": _std("string"),
    "i": _std("istream"),
    "o": _std("ostream"),
    "d": _std("iostream"),
}

# verboseAST maps standard substitution codes to the long form of the
# corresponding AST.  We use this when the Verbose option is used, to
# match the standard demangler (and for constructors/destructors).
VERBOSE_AST = {
    "t": Name("std"),
    "a": _std("allocator"),
    "b": _std("basic_string"),
    # std::basic_string<char, std::char_traits<char>, std::allocator<char> >
    "s": Template(
        _std("basic_string"),
        [
            BuiltinType("char"),
            Template(_std("char_traits"), [BuiltinType("char")]),
            Template(_std("allocator"), [BuiltinType("char")]),
        ],
    ),
    # std::basic_istream<char, std::char_traits<char> >
    "i": Template(
        _std("basic_istream"),
        [BuiltinType("char"), Template(_std("char_traits"), [BuiltinType("char")])],
    ),
    # std::basic_ostream<char, std::char_traits<char> >
    "o": Template(
        _std("basic_ostream"),
        [BuiltinType("char"), Template(_std("char_traits"), [BuiltinType("char")])],
    ),
    # std::basic_iostream<char, std::char_traits<char> >
    "d": Template(
        _std("basic_iostream"),
        [BuiltinType("char"), Template(_std("char_traits"), [BuiltinType("char")])],
    ),
}

_ENABLE_IF_PREFIX = "Ua9enable_ifI"
_ANON_PREFIX = "_GLOBAL_"


class State:
    """A state holds the current state of demangling a string."""

    __slots__ = (
        "s",
        "n",
        "pos",
        "subs",
        "templates",
        "lambda_template_level",
        "parsing_constraint",
        "type_template_param_count",
        "non_type_template_param_count",
        "template_template_param_count",
    )

    def __init__(self, name: str) -> None:
        self.s = name + "\0"  # string to demangle, plus sentinel
        self.n = len(name)
        self.pos = 0  # offset of the remainder (Go's st.off)
        self.subs: List[AST] = []  # substitutions
        self.templates: List[Optional[Template]] = []  # templates being processed

        # The number of entries in templates when we started parsing
        # a lambda, plus 1 so that 0 means not parsing a lambda.
        self.lambda_template_level = 0

        self.parsing_constraint = False  # whether parsing a constraint expression

        # Counts of template parameters without template arguments,
        # for lambdas.
        self.type_template_param_count = 0
        self.non_type_template_param_count = 0
        self.template_template_param_count = 0

    # Go's st.copy() and *st = *save.
    def save(self):
        return (
            self.pos,
            len(self.subs),
            list(self.templates),
            self.lambda_template_level,
            self.parsing_constraint,
            self.type_template_param_count,
            self.non_type_template_param_count,
            self.template_template_param_count,
        )

    def restore(self, save) -> None:
        (
            self.pos,
            nsubs,
            self.templates,
            self.lambda_template_level,
            self.parsing_constraint,
            self.type_template_param_count,
            self.non_type_template_param_count,
            self.template_template_param_count,
        ) = save
        del self.subs[nsubs:]

    def fail(self, err: str):
        """Raise a demangleErr at the current offset."""
        raise DemangleError(err, self.pos)

    def fail_earlier(self, err: str, dec: int):
        """Like fail, but decrements the offset to indicate that the
        point of failure occurred earlier in the string."""
        if self.pos < dec:
            raise InternalError("internal error")
        raise DemangleError(err, self.pos - dec)

    def advance(self, add: int) -> None:
        """Advance the current string offset."""
        if self.n - self.pos < add:
            raise InternalError("internal error")
        self.pos += add

    def check_char(self, c: str) -> None:
        """Require that the next character in the string be c, and
        advance past it."""
        if self.pos >= self.n or self.s[self.pos] != c:
            raise InternalError("internal error")
        self.pos += 1

    def rem(self) -> int:
        """len(st.str)"""
        return self.n - self.pos

    # encoding parses:
    #
    #	encoding ::= <(function) name> <bare-function-type>
    #	             <(data) name>
    #	             <special-name>
    def encoding(self, local: int) -> AST:
        if self.pos >= self.n:
            self.fail("expected encoding")

        c = self.s[self.pos]
        if c == "G" or c == "T":
            return self.special_name()

        a, explicit_object_parameter = self.name()
        a = simplify(a)

        # (The !params case of upstream is only used with NoParams.)

        if self.pos >= self.n or self.s[self.pos] == "E":
            # There are no parameters--this is a data symbol, not
            # a function symbol.
            return a

        mwq = a if isinstance(a, MethodWithQualifiers) else None

        template = _find_template(a)
        old_lambda_template_level = 0
        if template is not None:
            self.templates.append(template)
            old_lambda_template_level = self.lambda_template_level
            self.lambda_template_level = 0

        # Checking for the enable_if attribute here is what the LLVM
        # demangler does.  This is not very general but perhaps it is
        # sufficient.
        enable_if_args: List[AST] = []
        enable_if_constraint = None
        if self.s.startswith(_ENABLE_IF_PREFIX, self.pos):
            self.advance(len(_ENABLE_IF_PREFIX) - 1)
            enable_if_args, enable_if_constraint = self.template_args()

        ft = self.bare_function_type(has_return_type(a), explicit_object_parameter)

        constraint = None
        if self.s[self.pos] == "Q":
            constraint = self.constraint_expr()

        if template is not None:
            self.templates.pop()
            self.lambda_template_level = old_lambda_template_level

        ft = simplify(ft)

        # For a local name, discard the return type, so that it
        # doesn't get confused with the top level return type.
        if local == FOR_LOCAL_NAME:
            if isinstance(ft, FunctionType):
                ft.for_local_name = True

        # Any top-level qualifiers belong to the function type.
        if mwq is not None:
            a = mwq.method
            mwq.method = ft
            ft = mwq
        if isinstance(a, Qualified) and a.local_name:
            q = a
            holder = q
            p = q.name
            if isinstance(p, DefaultArg):
                holder = p
                p = p.arg
            if isinstance(p, MethodWithQualifiers):
                inner_mwq = p
                if holder is q:
                    q.name = inner_mwq.method
                else:
                    holder.arg = inner_mwq.method
                inner_mwq.method = ft
                ft = inner_mwq

        # Any top-level constraint belongs on the function type.
        for_template_args = False
        if isinstance(a, Constraint) and constraint is None:
            c = a
            a = c.name
            constraint = c.requires
            for_template_args = c.for_template_args

        r: AST = Typed(a, ft)

        if enable_if_args:
            r = EnableIf(r, enable_if_args)
            r = self.maybe_add_constraint(r, enable_if_constraint)

        if constraint is not None:
            r = Constraint(r, constraint, for_template_args)

        return r

    # taggedName parses:
    #
    #	<tagged-name> ::= <name> B <source-name>
    def tagged_name(self, a: AST) -> AST:
        while self.s[self.pos] == "B":
            self.advance(1)
            tag = self.source_name()
            a = TaggedName(a, tag)
        return a

    # name parses:
    #
    #	<name> ::= <nested-name>
    #	       ::= <unscoped-name>
    #	       ::= <unscoped-template-name> <template-args>
    #	       ::= <local-name>
    #
    #	<unscoped-name> ::= <unqualified-name>
    #	                ::= St <unqualified-name>
    #
    #	<unscoped-template-name> ::= <unscoped-name>
    #	                         ::= <substitution>
    #
    # Besides the name, this returns whether it saw the code indicating
    # a C++23 explicit object parameter.
    def name(self) -> Tuple[AST, bool]:
        if self.pos >= self.n:
            self.fail("expected name")

        module = None
        c = self.s[self.pos]
        if c == "N":
            return self.nested_name()
        elif c == "Z":
            return self.local_name()
        elif c == "U":
            a, is_cast = self.unqualified_name(None)
            if is_cast:
                self.set_template(a, None)
            return a, False
        elif c == "S":
            if self.n - self.pos < 2:
                self.advance(1)
                self.fail("expected substitution index")
            is_cast = False
            subst = False
            is_module = False
            if self.s[self.pos + 1] == "t":
                self.advance(2)
                a, is_cast = self.unqualified_name(None)
                a = Qualified(Name("std"), a, False)
            else:
                a = self.substitution(False)
                if isinstance(a, ModuleName):
                    module = a
                    is_module = True
                else:
                    subst = True
            if not is_module:
                constraint = None
                if self.s[self.pos] == "I":
                    # This can only happen if we saw
                    # <unscoped-template-name> and are about to see
                    # <template-args>.  <unscoped-template-name> is a
                    # substitution candidate if it did not come from a
                    # substitution.
                    if not subst:
                        self.subs.append(a)
                    args, constraint = self.template_args()
                    tmpl = Template(a, args)
                    if is_cast:
                        self.set_template(a, tmpl)
                        self.clear_template_args(args)
                        is_cast = False
                    a = tmpl
                if is_cast:
                    self.set_template(a, None)
                a = self.maybe_add_constraint(a, constraint)
                return a, False

        a, is_cast = self.unqualified_name(module)
        constraint = None
        if self.s[self.pos] == "I":
            self.subs.append(a)
            args, constraint = self.template_args()
            tmpl = Template(a, args)
            if is_cast:
                self.set_template(a, tmpl)
                self.clear_template_args(args)
                is_cast = False
            a = tmpl
        if is_cast:
            self.set_template(a, None)
        a = self.maybe_add_constraint(a, constraint)
        return a, False

    # nestedName parses:
    #
    #	<nested-name> ::= N [<CV-qualifiers>] [<ref-qualifier>] <prefix> <unqualified-name> E
    #	              ::= N [<CV-qualifiers>] [<ref-qualifier>] <template-prefix> <template-args> E
    #
    # Besides the name, this returns whether it saw the code indicating
    # a C++23 explicit object parameter.
    def nested_name(self) -> Tuple[AST, bool]:
        self.check_char("N")

        q = None
        r = ""

        explicit_object_parameter = False
        if self.s[self.pos] == "H":
            self.advance(1)
            explicit_object_parameter = True
        else:
            q = self.cv_qualifiers()
            r = self.ref_qualifier()

        a = self.prefix()

        if q is not None or r != "":
            a = MethodWithQualifiers(a, q, r)
        if self.s[self.pos] != "E":
            self.fail("expected E after nested name")
        self.advance(1)
        return a, explicit_object_parameter

    # prefix parses:
    #
    #	<prefix> ::= <prefix> <unqualified-name>
    #	         ::= <template-prefix> <template-args>
    #	         ::= <template-param>
    #	         ::= <decltype>
    #	         ::=
    #	         ::= <substitution>
    #
    #	<template-prefix> ::= <prefix> <(template) unqualified-name>
    #	                  ::= <template-param>
    #	                  ::= <substitution>
    #
    #	<decltype> ::= Dt <expression> E
    #	           ::= DT <expression> E
    def prefix(self) -> AST:
        a = None

        # The last name seen, for a constructor/destructor.
        last = None

        module = None

        def get_last(a):
            while True:
                if isinstance(a, Template):
                    a = a.name
                elif isinstance(a, Qualified):
                    a = a.name
                elif isinstance(a, TaggedName):
                    a = a.name
                else:
                    return a

        s = self.s
        cast = None
        while True:
            if self.pos >= self.n:
                self.fail("expected prefix")
            next_ = None

            c = s[self.pos]
            if (
                is_digit(c)
                or is_lower(c)
                or c == "U"
                or c == "L"
                or c == "F"
                or c == "W"
                or (c == "D" and self.n - self.pos > 1 and s[self.pos + 1] == "C")
            ):
                un, is_un_cast = self.unqualified_name(module)
                next_ = un
                module = None
                if is_un_cast:
                    if isinstance(un, TaggedName):
                        un = un.name
                    if not isinstance(un, Cast):
                        # Go's failing type assertion panics.
                        raise InternalError("interface conversion")
                    cast = un
            elif c == "C":
                inheriting = False
                self.advance(1)
                if s[self.pos] == "I":
                    inheriting = True
                    self.advance(1)
                if self.pos >= self.n:
                    self.fail("expected constructor type")
                if last is None:
                    self.fail("constructor before name is seen")
                self.advance(1)
                base = None
                if inheriting:
                    base = self.demangle_type(False)
                next_ = Constructor(get_last(last), base)
                if s[self.pos] == "B":
                    next_ = self.tagged_name(next_)
            elif c == "D":
                if self.n - self.pos > 1 and (s[self.pos + 1] == "T" or s[self.pos + 1] == "t"):
                    next_ = self.demangle_type(False)
                else:
                    if self.n - self.pos < 2:
                        self.fail("expected destructor type")
                    if last is None:
                        self.fail("destructor before name is seen")
                    self.advance(2)
                    next_ = Destructor(get_last(last))
                    if s[self.pos] == "B":
                        next_ = self.tagged_name(next_)
            elif c == "S":
                next_ = self.substitution(True)
                if isinstance(next_, ModuleName):
                    module = next_
                    next_ = None
            elif c == "I":
                if a is None:
                    self.fail("unexpected template arguments")
                args, constraint = self.template_args()
                tmpl = Template(a, args)
                if cast is not None:
                    self.set_template(cast, tmpl)
                    self.clear_template_args(args)
                    cast = None
                a = None
                next_ = self.maybe_add_constraint(tmpl, constraint)
            elif c == "T":
                next_ = self.template_param()
            elif c == "E":
                if a is None:
                    self.fail("expected prefix")
                if cast is not None:
                    to_tmpl = None
                    if isinstance(cast.to, Template):
                        to_tmpl = cast.to
                    self.set_template(cast, to_tmpl)
                return a
            elif c == "M":
                if a is None:
                    self.fail("unexpected lambda initializer")
                # This is the initializer scope for a
                # lambda.  We don't need to record
                # it.  The normal code will treat the
                # variable has a type scope, which
                # gives appropriate output.
                self.advance(1)
                continue
            elif c == "J":
                # It appears that in some cases clang
                # can emit a J for a template arg
                # without the expected I.  I don't
                # know when this happens, but I've
                # seen it in some large C++ programs.
                if a is None:
                    self.fail("unexpected template arguments")
                args = []
                while self.pos >= self.n or s[self.pos] != "E":
                    arg = self.template_arg(None)
                    args.append(arg)
                self.advance(1)
                tmpl = Template(a, args)
                if cast is not None:
                    self.set_template(cast, tmpl)
                    self.clear_template_args(args)
                    cast = None
                a = None
                next_ = tmpl
            else:
                self.fail("unrecognized letter in prefix")

            if next_ is None:
                continue

            last = next_
            if a is None:
                a = next_
            else:
                a = Qualified(a, next_, False)

            if c != "S" and s[self.pos] != "E":
                self.subs.append(a)

    # unqualifiedName parses:
    #
    #	<unqualified-name> ::= <operator-name>
    #	                   ::= <ctor-dtor-name>
    #	                   ::= <source-name>
    #	                   ::= <local-source-name>
    #
    #	 <local-source-name>	::= L <source-name> <discriminator>
    def unqualified_name(self, module: Optional[AST]) -> Tuple[AST, bool]:
        module = self.module_name(module)

        if self.pos >= self.n:
            self.fail("expected unqualified name")

        s = self.s
        friend = False
        if s[self.pos] == "F":
            self.advance(1)
            friend = True
            if self.pos >= self.n:
                self.fail("expected unqualified name")

        is_cast = False
        c = s[self.pos]
        if is_digit(c):
            a = self.source_name()
        elif is_lower(c):
            a, _ = self.operator_name(False)
            if isinstance(a, Cast):
                is_cast = True
            if isinstance(a, Operator) and a.name == 'operator"" ':
                n = self.source_name()
                a = Unary(a, n, False, False)
        elif c == "D" and self.n - self.pos > 1 and s[self.pos + 1] == "C":
            bindings = []
            self.advance(2)
            while True:
                binding = self.source_name()
                bindings.append(binding)
                if s[self.pos] == "E":
                    self.advance(1)
                    break
            a = StructuredBindings(bindings)
        else:
            if c == "C" or c == "D":
                self.fail("constructor/destructor not in nested name")
            elif c == "L":
                self.advance(1)
                a = self.source_name()
                a = self.discriminator(a)
            elif c == "U":
                if self.n - self.pos < 2:
                    self.advance(1)
                    self.fail("expected closure or unnamed type")
                c = s[self.pos + 1]
                if c == "b":
                    self.advance(2)
                    self.compact_number()
                    a = Name("'block-literal'")
                elif c == "l":
                    a = self.closure_type_name()
                elif c == "t":
                    a = self.unnamed_type_name()
                else:
                    self.advance(1)
                    self.fail("expected closure or unnamed type")
            else:
                self.fail("expected unqualified name")

        if module is not None:
            a = ModuleEntity(module, a)

        if s[self.pos] == "B":
            a = self.tagged_name(a)

        if friend:
            a = Friend(a)

        return a, is_cast

    # sourceName parses:
    #
    #	<source-name> ::= <(positive length) number> <identifier>
    #	identifier ::= <(unqualified source code identifier)>
    def source_name(self) -> AST:
        val = self.number()
        if val <= 0:
            self.fail("expected positive number")
        if self.n - self.pos < val:
            self.fail("not enough characters for identifier")
        id_ = self.s[self.pos : self.pos + val]
        self.advance(val)

        # Look for GCC encoding of anonymous namespace, and make it
        # more friendly.
        if id_.startswith(_ANON_PREFIX) and len(id_) > len(_ANON_PREFIX) + 2:
            c1 = id_[len(_ANON_PREFIX)]
            c2 = id_[len(_ANON_PREFIX) + 1]
            if (c1 == "." or c1 == "_" or c1 == "$") and c2 == "N":
                id_ = "(anonymous namespace)"

        return Name(id_)

    # moduleName parses:
    #
    #	<module-name> ::= <module-subname>
    #	 	      ::= <module-name> <module-subname>
    #		      ::= <substitution>  # passed in by caller
    #	<module-subname> ::= W <source-name>
    #			 ::= W P <source-name>
    #
    # The module name is optional. If it is not present, this returns the parent.
    def module_name(self, parent: Optional[AST]) -> Optional[AST]:
        ret = parent
        while self.s[self.pos] == "W":
            self.advance(1)
            is_partition = False
            if self.s[self.pos] == "P":
                self.advance(1)
                is_partition = True
            name = self.source_name()
            ret = ModuleName(ret, name, is_partition)
            self.subs.append(ret)
        return ret

    # number parses:
    #
    #	number ::= [n] <(non-negative decimal integer)>
    def number(self) -> int:
        s = self.s
        neg = False
        if s[self.pos] == "n":
            neg = True
            self.advance(1)
        if not is_digit(s[self.pos]):
            self.fail("missing number")
        val = 0
        while is_digit(s[self.pos]):
            # Number picked to ensure we can't overflow with 32-bit int.
            # Any very large number here is bogus.
            if val >= 0x80000000 // 10 - 10:
                self.fail("numeric overflow")
            val = val * 10 + ord(s[self.pos]) - 48
            self.pos += 1
        if neg:
            val = -val
        return val

    # seqID parses:
    #
    #	<seq-id> ::= <0-9A-Z>+
    #
    # We expect this to be followed by an underscore.
    def seq_id(self, eof_ok: bool) -> int:
        s = self.s
        if s[self.pos] == "_":
            self.advance(1)
            return 0
        id_ = 0
        while True:
            if self.pos >= self.n:
                if eof_ok:
                    return id_ + 1
                self.fail("missing end to sequence ID")
            # Don't overflow a 32-bit int.
            if id_ >= 0x80000000 // 36 - 36:
                self.fail("sequence ID overflow")
            c = s[self.pos]
            if c == "_":
                self.advance(1)
                return id_ + 1
            if is_digit(c):
                id_ = id_ * 36 + ord(c) - 48
            elif is_upper(c):
                id_ = id_ * 36 + ord(c) - 65 + 10
            else:
                self.fail("invalid character in sequence ID")
            self.advance(1)

    # operatorName parses:
    #
    #	operator_name ::= many different two character encodings.
    #	              ::= cv <type>
    #	              ::= v <digit> <source-name>
    #
    # We need to know whether we are in an expression because it affects
    # how we handle template parameters in the type of a cast operator.
    def operator_name(self, in_expression: bool) -> Tuple[AST, int]:
        if self.n - self.pos < 2:
            self.fail("missing operator code")
        code = self.s[self.pos : self.pos + 2]
        self.advance(2)
        if code[0] == "v" and is_digit(code[1]):
            name = self.source_name()
            return Operator(name.name), ord(code[1]) - 48
        elif code == "cv":
            # Push a None on templates to indicate that template
            # parameters will have their template filled in
            # later.
            if not in_expression:
                self.templates.append(None)

            t = self.demangle_type(not in_expression)

            if not in_expression:
                self.templates.pop()

            return Cast(t), 1
        else:
            op = OPERATORS.get(code)
            if op is not None:
                return Operator(op[0]), op[1]
            self.fail_earlier("unrecognized operator code", 2)

    # localName parses:
    #
    #	<local-name> ::= Z <(function) encoding> E <(entity) name> [<discriminator>]
    #	             ::= Z <(function) encoding> E s [<discriminator>]
    #	             ::= Z <(function) encoding> E d [<parameter> number>] _ <entity name>
    #
    # Besides the name, this returns whether it saw the code indicating
    # a C++23 explicit object parameter.
    def local_name(self) -> Tuple[AST, bool]:
        self.check_char("Z")
        fn = self.encoding(FOR_LOCAL_NAME)
        if self.s[self.pos] != "E":
            self.fail("expected E after local name")
        self.advance(1)
        if self.s[self.pos] == "s":
            self.advance(1)
            n: AST = Name("string literal")
            n = self.discriminator(n)
            return Qualified(fn, n, True), False
        else:
            num = -1
            if self.s[self.pos] == "d":
                # Default argument scope.
                self.advance(1)
                num = self.compact_number()
            n, explicit_object_parameter = self.name()
            n = self.discriminator(n)
            if num >= 0:
                n = DefaultArg(num, n)
            return Qualified(fn, n, True), explicit_object_parameter

    def java_resource(self) -> AST:
        """Parse a Java resource special-name."""
        off = self.pos
        ln = self.number()
        if ln <= 1:
            self.fail_earlier("java resource length less than 1", self.pos - off)
        if self.s[self.pos] != "_":
            self.fail("expected _ after number")
        self.advance(1)
        ln -= 1
        if self.n - self.pos < ln:
            self.fail("not enough characters for java resource length")
        str_ = self.s[self.pos : self.pos + ln]
        final = []
        self.advance(ln)
        i = 0
        while i < len(str_):
            if str_[i] != "$":
                final.append(str_[i])
            else:
                if len(str_) <= i + 1:
                    self.fail_earlier("java resource escape at end of string", 1)
                i += 1
                r = {"S": "/", "_": ".", "$": "$"}.get(str_[i])
                if r is None:
                    self.fail_earlier("unrecognized java resource escape", ln - i - 1)
                final.append(r)
            i += 1
        return Special("java resource ", Name("".join(final)))

    # specialName parses:
    #
    #	<special-name> ::= TV <type>
    #	               ::= TT <type>
    #	               ::= TI <type>
    #	               ::= TS <type>
    #	               ::= TA <template-arg>
    #	               ::= GV <(object) name>
    #	               ::= T <call-offset> <(base) encoding>
    #	               ::= Tc <call-offset> <call-offset> <(base) encoding>
    #	g++ extensions:
    #	               ::= TC <type> <(offset) number> _ <(base) type>
    #	               ::= TF <type>
    #	               ::= TJ <type>
    #	               ::= GR <name>
    #	               ::= GA <encoding>
    #	               ::= Gr <resource name>
    #	               ::= GTt <encoding>
    #	               ::= GTn <encoding>
    #	               ::= GI <module name>
    def special_name(self) -> AST:
        if self.s[self.pos] == "T":
            self.advance(1)
            if self.pos >= self.n:
                self.fail("expected special name code")
            c = self.s[self.pos]
            self.advance(1)
            if c == "V":
                t = self.demangle_type(False)
                return Special("vtable for ", t)
            elif c == "T":
                t = self.demangle_type(False)
                return Special("VTT for ", t)
            elif c == "I":
                t = self.demangle_type(False)
                return Special("typeinfo for ", t)
            elif c == "S":
                t = self.demangle_type(False)
                return Special("typeinfo name for ", t)
            elif c == "A":
                t = self.template_arg(None)
                return Special("template parameter object for ", t)
            elif c == "h":
                self.call_offset("h")
                v = self.encoding(NOT_FOR_LOCAL_NAME)
                return Special("non-virtual thunk to ", v)
            elif c == "v":
                self.call_offset("v")
                v = self.encoding(NOT_FOR_LOCAL_NAME)
                return Special("virtual thunk to ", v)
            elif c == "c":
                self.call_offset("")
                self.call_offset("")
                v = self.encoding(NOT_FOR_LOCAL_NAME)
                return Special("covariant return thunk to ", v)
            elif c == "C":
                derived = self.demangle_type(False)
                off = self.pos
                offset = self.number()
                if offset < 0:
                    self.fail_earlier("expected positive offset", self.pos - off)
                if self.s[self.pos] != "_":
                    self.fail("expected _ after number")
                self.advance(1)
                base = self.demangle_type(False)
                return Special2("construction vtable for ", base, "-in-", derived)
            elif c == "F":
                t = self.demangle_type(False)
                return Special("typeinfo fn for ", t)
            elif c == "J":
                t = self.demangle_type(False)
                return Special("java Class for ", t)
            elif c == "H":
                n, _ = self.name()
                return Special("TLS init function for ", n)
            elif c == "W":
                n, _ = self.name()
                return Special("TLS wrapper function for ", n)
            else:
                self.fail("unrecognized special T name code")
        else:
            self.check_char("G")
            if self.pos >= self.n:
                self.fail("expected special name code")
            c = self.s[self.pos]
            self.advance(1)
            if c == "V":
                n, _ = self.name()
                return Special("guard variable for ", n)
            elif c == "R":
                n, _ = self.name()
                self.seq_id(True)
                return Special("reference temporary for ", n)
            elif c == "A":
                v = self.encoding(NOT_FOR_LOCAL_NAME)
                return Special("hidden alias for ", v)
            elif c == "T":
                if self.pos >= self.n:
                    self.fail("expected special GT name code")
                c = self.s[self.pos]
                self.advance(1)
                v = self.encoding(NOT_FOR_LOCAL_NAME)
                if c == "n":
                    return Special("non-transaction clone for ", v)
                # The proposal is that different
                # letters stand for different types
                # of transactional cloning.  Treat
                # them all the same for now.
                return Special("transaction clone for ", v)
            elif c == "r":
                return self.java_resource()
            elif c == "I":
                module = self.module_name(None)
                if module is None:
                    self.fail("expected module after GI")
                return Special("initializer for module ", module)
            else:
                self.fail("unrecognized special G name code")

    # callOffset parses:
    #
    #	<call-offset> ::= h <nv-offset> _
    #	              ::= v <v-offset> _
    #
    #	<nv-offset> ::= <(offset) number>
    #
    #	<v-offset> ::= <(offset) number> _ <(virtual offset) number>
    #
    # The c parameter, if not "", is a character we just read which is the
    # start of the <call-offset>.
    #
    # We don't display the offset information anywhere.
    def call_offset(self, c: str) -> None:
        if c == "":
            if self.pos >= self.n:
                self.fail("missing call offset")
            c = self.s[self.pos]
            self.advance(1)
        if c == "h":
            self.number()
        elif c == "v":
            self.number()
            if self.s[self.pos] != "_":
                self.fail("expected _ after number")
            self.advance(1)
            self.number()
        else:
            self.fail_earlier("unrecognized call offset code", 1)
        if self.s[self.pos] != "_":
            self.fail("expected _ after call offset")
        self.advance(1)

    # demangleType parses:
    #
    #	<type> ::= <builtin-type>
    #	       ::= <function-type>
    #	       ::= <class-enum-type>
    #	       ::= <array-type>
    #	       ::= <pointer-to-member-type>
    #	       ::= <template-param>
    #	       ::= <template-template-param> <template-args>
    #	       ::= <substitution>
    #	       ::= <CV-qualifiers> <type>
    #	       ::= P <type>
    #	       ::= R <type>
    #	       ::= O <type> (C++0x)
    #	       ::= C <type>
    #	       ::= G <type>
    #	       ::= U <source-name> <type>
    #
    #	<builtin-type> ::= various one letter codes
    #	               ::= u <source-name>
    def demangle_type(self, is_cast: bool) -> AST:
        if self.pos >= self.n:
            self.fail("expected type")

        s = self.s
        add_subst = True

        q = self.cv_qualifiers()
        if q is not None:
            if self.pos >= self.n:
                self.fail("expected type")

            # CV-qualifiers before a function type apply to
            # 'this', so avoid adding the unqualified function
            # type to the substitution list.
            if s[self.pos] == "F":
                add_subst = False

        # Use correct substitution for a template parameter.
        # (Go's `sub` variable is never set; it is always nil.)

        c = s[self.pos]
        btype = BUILTIN_TYPES.get(c)
        if btype is not None:
            ret: AST = BuiltinType(btype)
            self.advance(1)
            if q is not None:
                ret = TypeWithQualifiers(ret, q)
                self.subs.append(ret)
            return ret

        if c == "u":
            self.advance(1)
            ret = self.source_name()
            if s[self.pos] == "I":
                self.advance(1)
                base = self.demangle_type(False)
                if s[self.pos] != "E":
                    self.fail("expected E after transformed type")
                self.advance(1)
                ret = TransformedType(ret.name, base)
        elif c == "F":
            ret = self.function_type()
        elif c in "NWZ0123456789":
            ret, _ = self.name()
        elif c == "A":
            ret = self.array_type(is_cast)
        elif c == "M":
            ret = self.pointer_to_member_type(is_cast)
        elif c == "T":
            if self.n - self.pos > 1 and s[self.pos + 1] in "sue":
                c = s[self.pos + 1]
                self.advance(2)
                ret, _ = self.name()
                if c == "s":
                    kind = "struct"
                elif c == "u":
                    kind = "union"
                else:
                    kind = "enum"
                ret = ElaboratedType(kind, ret)
            else:
                ret = self.template_param()
                if s[self.pos] == "I":
                    # See the function comment to explain this.
                    if not is_cast:
                        self.subs.append(ret)
                        ret = self.template(ret)
                    else:
                        ret = self.demangle_cast_template_args(ret, True)
        elif c == "S":
            # If this is a special substitution, then it
            # is the start of <class-enum-type>.
            c2 = ""
            if self.n - self.pos > 1:
                c2 = s[self.pos + 1]
            if is_digit(c2) or c2 == "_" or is_upper(c2):
                ret = self.substitution(False)
                if isinstance(ret, ModuleName):
                    ret, _ = self.unqualified_name(ret)
                    self.subs.append(ret)
                if s[self.pos] != "I":
                    add_subst = False
                else:
                    # See the function comment to explain this.
                    if not isinstance(ret, TemplateParam) or not is_cast:
                        ret = self.template(ret)
                    else:
                        next_ = self.demangle_cast_template_args(ret, False)
                        if next_ is ret:
                            add_subst = False
                        ret = next_
            else:
                ret, _ = self.name()
                # This substitution is not itself a
                # substitution candidate, unless template
                # arguments were added.
                if ret is SUB_AST.get(c2) or ret is VERBOSE_AST.get(c2):
                    add_subst = False
        elif c in "OPRCG":
            self.advance(1)
            t = self.demangle_type(is_cast)
            if c == "O":
                ret = RvalueReferenceType(t)
            elif c == "P":
                ret = PointerType(t)
            elif c == "R":
                ret = ReferenceType(t)
            elif c == "C":
                ret = ComplexType(t)
            else:
                ret = ImaginaryType(t)
        elif c == "U":
            if self.n - self.pos < 2:
                self.fail("expected source name or unnamed type")
            c1 = s[self.pos + 1]
            if c1 == "l":
                ret = self.closure_type_name()
                add_subst = False
            elif c1 == "t":
                ret = self.unnamed_type_name()
                add_subst = False
            else:
                self.advance(1)
                n = self.source_name()
                if s[self.pos] == "I":
                    n = self.template(n)
                t = self.demangle_type(is_cast)
                ret = VendorQualifier(n, t)
        elif c == "D":
            self.advance(1)
            if self.pos >= self.n:
                self.fail("expected D code for type")
            add_subst = False
            c2 = s[self.pos]
            self.advance(1)

            if c2 == "T" or c2 == "t":
                # decltype(expression)
                ret = self.expression()
                if s[self.pos] != "E":
                    self.fail("expected E after expression in type")
                self.advance(1)
                ret = Decltype(ret)
                add_subst = True

            elif c2 == "p":
                t = self.demangle_type(is_cast)
                pack = self.find_argument_pack(t)
                ret = PackExpansion(t, pack)
                add_subst = True

            elif c2 == "a":
                ret = Name("auto")
            elif c2 == "c":
                ret = Name("decltype(auto)")

            elif c2 == "f":
                ret = BuiltinType("decimal32")
            elif c2 == "d":
                ret = BuiltinType("decimal64")
            elif c2 == "e":
                ret = BuiltinType("decimal128")
            elif c2 == "h":
                ret = BuiltinType("half")
            elif c2 == "u":
                ret = BuiltinType("char8_t")
            elif c2 == "s":
                ret = BuiltinType("char16_t")
            elif c2 == "i":
                ret = BuiltinType("char32_t")
            elif c2 == "n":
                ret = BuiltinType("decltype(nullptr)")

            elif c2 == "F":
                accum = False
                bits = 0
                if is_digit(s[self.pos]):
                    accum = True
                    bits = self.number()
                if s[self.pos] == "_":
                    if bits == 0:
                        self.fail("expected non-zero number of bits")
                    self.advance(1)
                    ret = BinaryFP(bits, "")
                elif s[self.pos] == "x":
                    if bits == 0:
                        self.fail("expected non-zero number of bits")
                    self.advance(1)
                    ret = BinaryFP(bits, "x")
                elif s[self.pos] == "b":
                    if bits != 16:
                        self.fail("expected bits to be 16 for std::bfloat16_t")
                    self.advance(1)
                    ret = BuiltinType("std::bfloat16_t")
                else:
                    base = self.demangle_type(is_cast)
                    if is_digit(s[self.pos]):
                        # We don't care about the bits.
                        self.number()
                    sat = False
                    if self.pos < self.n:
                        if s[self.pos] == "s":
                            sat = True
                        self.advance(1)
                    ret = FixedType(base, accum, sat)

            elif c2 == "v":
                ret = self.vector_type(is_cast)
                add_subst = True

            elif c2 == "B" or c2 == "U":
                signed = c2 == "B"
                if is_digit(s[self.pos]):
                    bits = self.number()
                    size: AST = Name("%d" % bits)
                else:
                    size = self.expression()
                if s[self.pos] != "_":
                    self.fail("expected _ after _BitInt size")
                self.advance(1)
                ret = BitIntType(size, signed)
                add_subst = True

            elif c2 == "k":
                constraint, _ = self.name()
                ret = SuffixType(constraint, "auto")

            elif c2 == "K":
                constraint, _ = self.name()
                ret = SuffixType(constraint, "decltype(auto)")

            elif c2 == "A" or c2 == "R":
                if self.pos >= self.n:
                    self.fail("missing D{A,R} code in type")
                prefix = _fixed_prefix(s[self.pos])
                if prefix is None:
                    self.fail("unrecognized D{A,R} code in type")
                self.advance(1)

                if c2 == "A":
                    ret = BuiltinType(prefix + "_Accum")
                else:
                    ret = BuiltinType(prefix + "_Fract")

            elif c2 == "S":
                if self.n - self.pos < 3:
                    self.fail("missing DS code in type")
                if s[self.pos] != "D":
                    self.fail("unrecognized DS code in type")

                c3 = s[self.pos + 1]
                if c3 == "A":
                    suffix = "_Accum"
                elif c3 == "R":
                    suffix = "_Fract"
                else:
                    self.fail("unrecognized DSD code in type")

                prefix = _fixed_prefix(s[self.pos + 2])
                if prefix is None:
                    self.fail("unrecognized DSD{A,R} code in type")

                self.advance(3)

                ret = BuiltinType("_Sat " + prefix + suffix)

            else:
                self.fail("unrecognized D code in type")

        else:
            self.fail("unrecognized type code")

        if add_subst:
            self.subs.append(ret)

        if q is not None:
            if isinstance(ret, FunctionType):
                ret = MethodWithQualifiers(ret, q, "")
            elif isinstance(ret, MethodWithQualifiers):
                # Merge adjacent qualifiers.  This case
                # happens with a function with a trailing
                # ref-qualifier.
                ret.qualifiers = merge_qualifiers(q, ret.qualifiers)
            else:
                # Merge adjacent qualifiers.  This case
                # happens with multi-dimensional array types.
                if isinstance(ret, TypeWithQualifiers):
                    q = merge_qualifiers(q, ret.qualifiers)
                    ret = ret.base
                ret = TypeWithQualifiers(ret, q)
            self.subs.append(ret)

        return ret

    # demangleCastTemplateArgs is for a rather hideous parse.  When we
    # see a template-param followed by a template-args, we need to decide
    # whether we have a template-param or a template-template-param.
    # Normally it is template-template-param, meaning that we pick up the
    # template arguments here.  But, if we are parsing the type for a
    # cast operator, then the only way this can be template-template-param
    # is if there is another set of template-args immediately after this
    # set.  That would look like this:
    #
    #	<nested-name>
    #	-> <template-prefix> <template-args>
    #	-> <prefix> <template-unqualified-name> <template-args>
    #	-> <unqualified-name> <template-unqualified-name> <template-args>
    #	-> <source-name> <template-unqualified-name> <template-args>
    #	-> <source-name> <operator-name> <template-args>
    #	-> <source-name> cv <type> <template-args>
    #	-> <source-name> cv <template-template-param> <template-args> <template-args>
    #
    # Otherwise, we have this derivation:
    #
    #	<nested-name>
    #	-> <template-prefix> <template-args>
    #	-> <prefix> <template-unqualified-name> <template-args>
    #	-> <unqualified-name> <template-unqualified-name> <template-args>
    #	-> <source-name> <template-unqualified-name> <template-args>
    #	-> <source-name> <operator-name> <template-args>
    #	-> <source-name> cv <type> <template-args>
    #	-> <source-name> cv <template-param> <template-args>
    #
    # in which the template-args are actually part of the prefix.  For
    # the special case where this arises, demangleType is called with
    # isCast as true.  This function is then responsible for checking
    # whether we see <template-param> <template-args> but there is not
    # another following <template-args>.  In that case, we reset the
    # parse and just return the <template-param>.
    def demangle_cast_template_args(self, tp: AST, add_subst: bool) -> AST:
        save = self.save()

        args = None
        constraint = None
        failed = False
        try:
            args, constraint = self.template_args()
        except DemangleError as e:
            if type(e) is not DemangleError:  # pragma: no cover
                raise
            failed = True

        if not failed and self.s[self.pos] == "I":
            if add_subst:
                self.subs.append(tp)
            ret = Template(tp, args)
            return self.maybe_add_constraint(ret, constraint)
        # Reset back to before we started reading the template arguments.
        # They will be read again by st.prefix.
        self.restore(save)
        return tp

    # cvQualifiers parses:
    #
    #	<CV-qualifiers> ::= [r] [V] [K]
    def cv_qualifiers(self) -> Optional[AST]:
        s = self.s
        q: List[AST] = []
        while self.pos < self.n:
            c = s[self.pos]
            qv = QUALIFIERS.get(c)
            if qv is not None:
                q.insert(0, Qualifier(qv))
                self.advance(1)
            elif self.n - self.pos > 1 and c == "D":
                c1 = s[self.pos + 1]
                if c1 == "x":
                    qual = Qualifier("transaction_safe")
                    self.advance(2)
                elif c1 == "o":
                    qual = Qualifier("noexcept")
                    self.advance(2)
                elif c1 == "O":
                    self.advance(2)
                    expr = self.expression()
                    if s[self.pos] != "E":
                        self.fail("expected E after computed noexcept expression")
                    self.advance(1)
                    qual = Qualifier("noexcept", [expr])
                elif c1 == "w":
                    self.advance(2)
                    parmlist = self.parmlist(False)
                    if s[self.pos] != "E":
                        self.fail("expected E after throw parameter list")
                    self.advance(1)
                    qual = Qualifier("throw", parmlist)
                else:
                    break
                q.insert(0, qual)
            else:
                break
        if not q:
            return None
        return Qualifiers(q)

    # refQualifier parses:
    #
    #	<ref-qualifier> ::= R
    #	                ::= O
    def ref_qualifier(self) -> str:
        c = self.s[self.pos]
        if c == "R":
            self.advance(1)
            return "&"
        elif c == "O":
            self.advance(1)
            return "&&"
        return ""

    # parmlist parses:
    #
    #	<type>+
    def parmlist(self, explicit_object_parameter: bool) -> List[AST]:
        s = self.s
        ret: List[AST] = []
        while True:
            if self.pos >= self.n:
                break
            c = s[self.pos]
            if c == "E" or c == ".":
                break
            if (c == "R" or c == "O") and self.n - self.pos > 1 and s[self.pos + 1] == "E":
                # This is a function ref-qualifier.
                break
            if c == "Q":
                # This is a requires clause.
                break
            ptype = self.demangle_type(False)

            if not ret and explicit_object_parameter:
                ptype = ExplicitObjectParameter(ptype)

            ret.append(ptype)

        # There should always be at least one type.  A function that
        # takes no arguments will have a single parameter type
        # "void".
        if not ret:
            self.fail("expected at least one type in type list")

        # Omit a single parameter type void.
        if len(ret) == 1:
            bt = ret[0]
            if isinstance(bt, BuiltinType) and bt.name == "void":
                ret = []

        return ret

    # functionType parses:
    #
    #	<function-type> ::= F [Y] <bare-function-type> [<ref-qualifier>] E
    def function_type(self) -> AST:
        self.check_char("F")
        if self.s[self.pos] == "Y":
            # Function has C linkage.  We don't print this.
            self.advance(1)
        ret: AST = self.bare_function_type(True, False)
        r = self.ref_qualifier()
        if r != "":
            ret = MethodWithQualifiers(ret, None, r)
        if self.s[self.pos] != "E":
            self.fail("expected E after function type")
        self.advance(1)
        return ret

    # bareFunctionType parses:
    #
    #	<bare-function-type> ::= [J]<type>+
    def bare_function_type(self, has_return_type: bool, explicit_object_parameter: bool) -> AST:
        if self.s[self.pos] == "J":
            has_return_type = True
            self.advance(1)
        return_type = None
        if has_return_type:
            return_type = self.demangle_type(False)
        types = self.parmlist(explicit_object_parameter)
        return FunctionType(return_type, types, False)  # may be set later in encoding

    # arrayType parses:
    #
    #	<array-type> ::= A <(positive dimension) number> _ <(element) type>
    #	             ::= A [<(dimension) expression>] _ <(element) type>
    def array_type(self, is_cast: bool) -> AST:
        self.check_char("A")

        if self.pos >= self.n:
            self.fail("missing array dimension")

        s = self.s
        if s[self.pos] == "_":
            dim: AST = Name("")
        elif is_digit(s[self.pos]):
            i = 1
            while is_digit(s[self.pos + i]):
                i += 1
            dim = Name(s[self.pos : self.pos + i])
            self.advance(i)
        else:
            dim = self.expression()

        if s[self.pos] != "_":
            self.fail("expected _ after dimension")
        self.advance(1)

        t = self.demangle_type(is_cast)

        arr = ArrayType(dim, t)

        # Qualifiers on the element of an array type go on the whole
        # array type.
        q = arr.element
        if isinstance(q, TypeWithQualifiers):
            return TypeWithQualifiers(ArrayType(dim, q.base), q.qualifiers)

        return arr

    # vectorType parses:
    #
    #	<vector-type> ::= Dv <number> _ <type>
    #	              ::= Dv _ <expression> _ <type>
    def vector_type(self, is_cast: bool) -> AST:
        if self.pos >= self.n:
            self.fail("expected vector dimension")

        if self.s[self.pos] == "_":
            self.advance(1)
            dim = self.expression()
        else:
            num = self.number()
            dim = Name("%d" % num)

        if self.s[self.pos] != "_":
            self.fail("expected _ after vector dimension")
        self.advance(1)

        t = self.demangle_type(is_cast)

        return VectorType(dim, t)

    # pointerToMemberType parses:
    #
    #	<pointer-to-member-type> ::= M <(class) type> <(member) type>
    def pointer_to_member_type(self, is_cast: bool) -> AST:
        self.check_char("M")
        cl = self.demangle_type(False)

        # The ABI says, "The type of a non-static member function is
        # considered to be different, for the purposes of
        # substitution, from the type of a namespace-scope or static
        # member function whose type appears similar. The types of
        # two non-static member functions are considered to be
        # different, for the purposes of substitution, if the
        # functions are members of different classes. In other words,
        # for the purposes of substitution, the class of which the
        # function is a member is considered part of the type of
        # function."
        #
        # For a pointer to member function, this call to demangleType
        # will end up adding a (possibly qualified) non-member
        # function type to the substitution table, which is not
        # correct; however, the member function type will never be
        # used in a substitution, so putting the wrong type in the
        # substitution table is harmless.
        mem = self.demangle_type(is_cast)
        return PtrMem(cl, mem)

    # compactNumber parses:
    #
    #	<non-negative number> _
    def compact_number(self) -> int:
        if self.pos >= self.n:
            self.fail("missing index")
        c = self.s[self.pos]
        if c == "_":
            self.advance(1)
            return 0
        elif c == "n":
            self.fail("unexpected negative number")
        n = self.number()
        if self.s[self.pos] != "_":
            self.fail("missing underscore after number")
        self.advance(1)
        return n + 1

    # templateParam parses:
    #
    #	<template-param> ::= T_
    #	                 ::= T <(parameter-2 non-negative) number> _
    #	                 ::= TL <level-1> __
    #	                 ::= TL <level-1> _ <parameter-2 non-negative number> _
    #
    # When a template parameter is a substitution candidate, any
    # reference to that substitution refers to the template parameter
    # with the same index in the currently active template, not to
    # whatever the template parameter would be expanded to here.  We sort
    # this out in substitution and simplify.
    def template_param(self) -> AST:
        off = self.pos
        self.check_char("T")

        level = 0
        if self.s[self.pos] == "L":
            self.advance(1)
            level = self.compact_number()

        n = self.compact_number()

        # We don't try to substitute template parameters in a
        # constraint expression.
        if self.parsing_constraint:
            return Name(self.s[off : self.pos - 1])

        if level >= len(self.templates):
            if self.lambda_template_level > 0 and level == self.lambda_template_level - 1:
                # Lambda auto params are mangled as template params.
                # See https://gcc.gnu.org/PR78252.
                return LambdaAuto(n)
            self.fail_earlier(
                "template parameter is not in scope of template (level %d >= %d)" % (level, len(self.templates)),
                self.pos - off,
            )

        template = self.templates[level]

        if template is None:
            # We are parsing a cast operator.  If the cast is
            # itself a template, then this is a forward
            # reference.  Fill it in later.
            return TemplateParam(n, None)

        if n >= len(template.args):
            if self.lambda_template_level > 0 and level == self.lambda_template_level - 1:
                # Lambda auto params are mangled as template params.
                # See https://gcc.gnu.org/PR78252.
                return LambdaAuto(n)
            self.fail_earlier(
                "template index out of range (%d >= %d)" % (n, len(template.args)),
                self.pos - off,
            )

        return TemplateParam(n, template)

    def set_template(self, a: AST, tmpl: Optional[Template]) -> None:
        """Set the Template field of any TemplateParam's in a.  This
        handles the forward referencing template parameters found in
        cast operators."""
        seen = set()

        def fn(a):
            if isinstance(a, TemplateParam):
                if a.template is not None:
                    if tmpl is not None:
                        self.fail("duplicate template parameters")
                    return False
                if tmpl is None:
                    self.fail("cast template parameter not in scope of template")
                if a.index >= len(tmpl.args):
                    self.fail("cast template index out of range (%d >= %d)" % (a.index, len(tmpl.args)))
                a.template = tmpl
                return False
            elif isinstance(a, Closure):
                # There are no template params in closure types.
                # https://gcc.gnu.org/PR78252.
                return False
            else:
                if a in seen:
                    return False
                seen.add(a)
                return True

        a.traverse(fn)

    def clear_template_args(self, args: List[AST]) -> None:
        """Give an error for any unset Template field in args.  This
        handles erroneous cases where a cast operator with a forward
        referenced template is in the scope of another cast operator."""
        for a in args:
            self.set_template(a, None)

    # templateArgs parses:
    #
    #	<template-args> ::= I <template-arg>+ E
    #
    # This returns the template arguments and an optional constraint.
    def template_args(self) -> Tuple[List[AST], Optional[AST]]:
        s = self.s
        if self.pos >= self.n or (s[self.pos] != "I" and s[self.pos] != "J"):
            raise InternalError("internal error")
        self.advance(1)

        ret: List[AST] = []
        constraint = None
        while self.pos >= self.n or s[self.pos] != "E":
            arg = self.template_arg(ret)
            ret.append(arg)

            if s[self.pos] == "Q":
                constraint = self.constraint_expr()
                if s[self.pos] != "E":
                    self.fail("expected end of template arguments after constraint")
        self.advance(1)
        return ret, constraint

    # templateArg parses:
    #
    #	<template-arg> ::= <type>
    #	               ::= X <expression> E
    #	               ::= <expr-primary>
    #	               ::= J <template-arg>* E
    #	               ::= LZ <encoding> E
    #	               ::= <template-param-decl> <template-arg>
    def template_arg(self, prev: Optional[List[AST]]) -> AST:
        if self.pos >= self.n:
            self.fail("missing template argument")
        s = self.s
        c = s[self.pos]
        if c == "X":
            self.advance(1)
            expr = self.expression()
            if s[self.pos] != "E":
                self.fail("missing end of expression")
            self.advance(1)
            return expr

        elif c == "L":
            return self.expr_primary()

        elif c == "I" or c == "J":
            args, constraint = self.template_args()
            ret = ArgumentPack(args)
            return self.maybe_add_constraint(ret, constraint)

        elif c == "T":
            arg = ""
            if self.n - self.pos > 1:
                arg = s[self.pos + 1]
            if arg in ("y", "n", "t", "p", "k"):
                off = self.pos

                # Apparently template references in the
                # template parameter refer to previous
                # arguments in the same template.
                # (Go shares the slice but its length is fixed;
                # copy the list so later appends are not seen.)
                template = Template(None, list(prev) if prev else [])
                self.templates.append(template)

                param, _ = self.template_param_decl()

                self.templates.pop()

                if param is None:
                    self.fail_earlier("expected template parameter as template argument", self.pos - off)
                arg_ast = self.template_arg(None)
                return TemplateParamQualifiedArg(param, arg_ast)
            return self.demangle_type(False)

        else:
            return self.demangle_type(False)

    def maybe_add_constraint(self, a: AST, constraint: Optional[AST]) -> AST:
        """Add a constraint to an AST, if constraint is not None.  This is
        only called with a constraint found at the end of template
        arguments."""
        if constraint is None:
            return a
        return Constraint(a, constraint, True)

    def template(self, name: AST) -> AST:
        """Parse template arguments into a Template with a given name."""
        args, constraint = self.template_args()
        ret = Template(name, args)
        return self.maybe_add_constraint(ret, constraint)

    def expr_list(self, stop: str) -> AST:
        """Parse a sequence of expressions up to a terminating character."""
        if self.s[self.pos] == stop:
            self.advance(1)
            return ExprList([])

        exprs = []
        while True:
            e = self.expression()
            exprs.append(e)
            if self.s[self.pos] == stop:
                self.advance(1)
                break
        return ExprList(exprs)

    # expression parses:
    #
    #	<expression> ::= <(unary) operator-name> <expression>
    #	             ::= <(binary) operator-name> <expression> <expression>
    #	             ::= <(trinary) operator-name> <expression> <expression> <expression>
    #	             ::= pp_ <expression>
    #	             ::= mm_ <expression>
    #	             ::= cl <expression>+ E
    #	             ::= cl <expression>+ E
    #	             ::= cv <type> <expression>
    #	             ::= cv <type> _ <expression>* E
    #	             ::= tl <type> <braced-expression>* E
    #	             ::= il <braced-expression>* E
    #	             ::= [gs] nw <expression>* _ <type> E
    #	             ::= [gs] nw <expression>* _ <type> <initializer>
    #	             ::= [gs] na <expression>* _ <type> E
    #	             ::= [gs] na <expression>* _ <type> <initializer>
    #	             ::= [gs] dl <expression>
    #	             ::= [gs] da <expression>
    #	             ::= dc <type> <expression>
    #	             ::= sc <type> <expression>
    #	             ::= cc <type> <expression>
    #	             ::= mc <parameter type> <expr> [<offset number>] E
    #	             ::= rc <type> <expression>
    #	             ::= ti <type>
    #	             ::= te <expression>
    #	             ::= so <referent type> <expr> [<offset number>] <union-selector>* [p] E
    #	             ::= st <type>
    #	             ::= sz <expression>
    #	             ::= at <type>
    #	             ::= az <expression>
    #	             ::= nx <expression>
    #	             ::= <template-param>
    #	             ::= <function-param>
    #	             ::= dt <expression> <unresolved-name>
    #	             ::= pt <expression> <unresolved-name>
    #	             ::= ds <expression> <expression>
    #	             ::= sZ <template-param>
    #	             ::= sZ <function-param>
    #	             ::= sP <template-arg>* E
    #	             ::= sp <expression>
    #	             ::= fl <binary operator-name> <expression>
    #	             ::= fr <binary operator-name> <expression>
    #	             ::= fL <binary operator-name> <expression> <expression>
    #	             ::= fR <binary operator-name> <expression> <expression>
    #	             ::= tw <expression>
    #	             ::= tr
    #	             ::= u <source-name> <template-arg>* E
    #	             ::= <unresolved-name>
    #	             ::= <expr-primary>
    #
    #	<function-param> ::= fp <CV-qualifiers> _
    #	                 ::= fp <CV-qualifiers> <number>
    #	                 ::= fL <number> p <CV-qualifiers> _
    #	                 ::= fL <number> p <CV-qualifiers> <number>
    #	                 ::= fpT
    #
    #	<braced-expression> ::= <expression>
    #	                    ::= di <field source-name> <braced-expression>
    #	                    ::= dx <index expression> <braced-expression>
    #	                    ::= dX <range begin expression> <range end expression> <braced-expression>
    def expression(self) -> AST:
        if self.pos >= self.n:
            self.fail("expected expression")
        s = self.s
        c = s[self.pos]
        has2 = self.n - self.pos > 1
        c1 = s[self.pos + 1] if has2 else ""
        if c == "L":
            return self.expr_primary()
        elif c == "T":
            return self.template_param()
        elif c == "s" and c1 == "o":
            self.advance(2)
            return self.subobject()
        elif c == "s" and c1 == "r":
            return self.unresolved_name()
        elif c == "s" and c1 == "p":
            self.advance(2)
            e = self.expression()
            pack = self.find_argument_pack(e)
            return PackExpansion(e, pack)
        elif c == "s" and c1 == "Z":
            self.advance(2)
            off = self.pos
            e = self.expression()
            ap = self.find_argument_pack(e)
            if ap is None:
                self.fail_earlier("missing argument pack", self.pos - off)
            return SizeofPack(ap)
        elif c == "s" and c1 == "P":
            self.advance(2)
            args = []
            while self.pos >= self.n or s[self.pos] != "E":
                arg = self.template_arg(None)
                args.append(arg)
            self.advance(1)
            return SizeofArgs(args)
        elif c == "f" and c1 == "p":
            self.advance(2)
            if s[self.pos] == "T":
                self.advance(1)
                return FunctionParam(0)
            else:
                # We can see qualifiers here, but we don't
                # include them in the demangled string.
                self.cv_qualifiers()
                index = self.compact_number()
                return FunctionParam(index + 1)
        elif c == "f" and self.n - self.pos > 2 and c1 == "L" and is_digit(s[self.pos + 2]):
            self.advance(2)
            # We don't include the scope count in the demangled string.
            self.number()
            if s[self.pos] != "p":
                self.fail("expected p after function parameter scope count")
            self.advance(1)
            # We can see qualifiers here, but we don't include them
            # in the demangled string.
            self.cv_qualifiers()
            index = self.compact_number()
            return FunctionParam(index + 1)
        elif c == "m" and c1 == "c":
            self.advance(2)
            typ = self.demangle_type(False)
            expr = self.expression()
            offset = 0
            if s[self.pos] == "n" or is_digit(s[self.pos]):
                offset = self.number()
            if s[self.pos] != "E":
                self.fail("expected E after pointer-to-member conversion")
            self.advance(1)
            return PtrMemCast(typ, expr, offset)
        elif is_digit(c) or (c == "o" and c1 == "n"):
            if c == "o":
                # Skip operator function ID.
                self.advance(2)
            n, _ = self.unqualified_name(None)
            if s[self.pos] == "I":
                n = self.template(n)
            return n
        elif (c == "i" or c == "t") and c1 == "l":
            # Brace-enclosed initializer list.
            self.advance(2)
            t = None
            if c == "t":
                t = self.demangle_type(False)
            exprs = self.expr_list("E")
            return InitializerList(t, exprs)
        elif c == "s" and c1 == "t":
            o, _ = self.operator_name(True)
            t = self.demangle_type(False)
            return Unary(o, t, False, True)
        elif c == "u":
            self.advance(1)
            name = self.source_name()
            # Special case __uuidof followed by type or
            # expression, as used by LLVM.
            if isinstance(name, Name) and name.name == "__uuidof":
                if self.n - self.pos < 2:
                    self.fail("missing uuidof argument")
                operand = None
                if s[self.pos] == "t":
                    self.advance(1)
                    operand = self.demangle_type(False)
                elif s[self.pos] == "z":
                    self.advance(1)
                    operand = self.expression()
                if operand is not None:
                    return Binary(Operator("()"), name, ExprList([operand]))
            args = []
            while True:
                if self.pos >= self.n:
                    self.fail("missing argument in vendor extended expressoin")
                if s[self.pos] == "E":
                    self.advance(1)
                    break
                arg = self.template_arg(None)
                args.append(arg)
            return Binary(Operator("()"), name, ExprList(args))
        elif c == "r" and (c1 == "q" or c1 == "Q"):
            return self.requires_expr()
        else:
            if self.n - self.pos < 2:
                self.fail("missing operator code")
            code = s[self.pos : self.pos + 2]
            o, args = self.operator_name(True)
            if args == 0:
                return Nullary(o)

            elif args == 1:
                suffix = False
                if code == "pp" or code == "mm":
                    if s[self.pos] == "_":
                        self.advance(1)
                    else:
                        suffix = True
                if isinstance(o, Cast) and s[self.pos] == "_":
                    self.advance(1)
                    operand = self.expr_list("E")
                else:
                    operand = self.expression()
                return Unary(o, operand, suffix, False)

            elif args == 2:
                right = None
                if code in ("sc", "dc", "cc", "rc"):
                    left = self.demangle_type(False)
                elif code[0] == "f":
                    left, _ = self.operator_name(True)
                    right = self.expression()
                    return Fold(code[1] == "l", left, right, None)
                elif code == "di":
                    left, _ = self.unqualified_name(None)
                else:
                    left = self.expression()
                if code == "cl" or code == "cp":
                    right = self.expr_list("E")
                elif code == "dt" or code == "pt":
                    if s[self.pos] == "L":
                        right = self.expr_primary()
                    else:
                        right = self.unresolved_name()
                        if s[self.pos] == "I":
                            right = self.template(right)
                else:
                    right = self.expression()
                return Binary(o, left, right)

            elif args == 3:
                if code[0] == "n":
                    if code[1] != "w" and code[1] != "a":
                        raise InternalError("internal error")
                    place = self.expr_list("_")
                    if not place.exprs:
                        place = None
                    t = self.demangle_type(False)
                    ini = None
                    if s[self.pos] == "E":
                        self.advance(1)
                    elif self.n - self.pos > 1 and s[self.pos] == "p" and s[self.pos + 1] == "i":
                        # Parenthesized initializer.
                        self.advance(2)
                        ini = self.expr_list("E")
                    elif self.n - self.pos > 1 and s[self.pos] == "i" and s[self.pos + 1] == "l":
                        # Initializer list.
                        ini = self.expression()
                    else:
                        self.fail("unrecognized new initializer")
                    return New(o, place, t, ini)
                elif code[0] == "f":
                    first, _ = self.operator_name(True)
                    second = self.expression()
                    third = self.expression()
                    return Fold(code[1] == "L", first, second, third)
                else:
                    first = self.expression()
                    second = self.expression()
                    third = self.expression()
                    return Trinary(o, first, second, third)

            else:
                self.fail("unsupported number of operator arguments: %d" % args)

    # subobject parses:
    #
    #	<expression> ::= so <referent type> <expr> [<offset number>] <union-selector>* [p] E
    #	<union-selector> ::= _ [<number>]
    def subobject(self) -> AST:
        s = self.s
        typ = self.demangle_type(False)
        expr = self.expression()
        offset = 0
        if s[self.pos] == "n" or is_digit(s[self.pos]):
            offset = self.number()
        selectors = []
        while s[self.pos] == "_":
            self.advance(1)
            selector = 0
            if s[self.pos] == "n" or is_digit(s[self.pos]):
                selector = self.number()
            selectors.append(selector)
        past_end = False
        if s[self.pos] == "p":
            self.advance(1)
            past_end = True
        if s[self.pos] != "E":
            self.fail("expected E after subobject")
        self.advance(1)
        return Subobject(typ, expr, offset, selectors, past_end)

    # unresolvedName parses:
    #
    #	<unresolved-name> ::= [gs] <base-unresolved-name>
    #	                  ::= sr <unresolved-type> <base-unresolved-name>
    #	                  ::= srN <unresolved-type> <unresolved-qualifier-level>+ E <base-unresolved-name>
    #	                  ::= [gs] sr <unresolved-qualifier-level>+ E <base-unresolved-name>
    def unresolved_name(self) -> AST:
        s = self.s
        if self.s.startswith("gs", self.pos) and self.n - self.pos >= 2:
            self.advance(2)
            n = self.unresolved_name()
            return Unary(Operator("::"), n, False, False)
        elif self.s.startswith("sr", self.pos) and self.n - self.pos >= 2:
            self.advance(2)
            if self.pos >= self.n:
                self.fail("expected unresolved type")
            c = s[self.pos]
            if c == "T" or c == "D" or c == "S":
                t = self.demangle_type(False)
                n = self.base_unresolved_name()
                n = Qualified(t, n, False)
                if s[self.pos] == "I":
                    n = self.template(n)
                    self.subs.append(n)
                return n
            else:
                sc = None
                if s[self.pos] == "N":
                    self.advance(1)
                    sc = self.demangle_type(False)
                while self.pos >= self.n or s[self.pos] != "E":
                    # GCC does not seem to follow the ABI here.
                    # It can emit type/name without an 'E'.
                    if sc is not None and self.pos < self.n and not is_digit(s[self.pos]):
                        if isinstance(sc, Qualified):
                            a = sc.scope
                            if isinstance(a, Template):
                                self.subs.append(a.name)
                                self.subs.append(a)
                            else:
                                self.subs.append(a)
                            return sc
                    n = self.source_name()
                    if s[self.pos] == "I":
                        self.subs.append(n)
                        n = self.template(n)
                    if sc is None:
                        sc = n
                    else:
                        sc = Qualified(sc, n, False)
                if sc is None:
                    self.fail("missing scope in unresolved name")
                self.advance(1)
                n = self.base_unresolved_name()
                return Qualified(sc, n, False)
        else:
            return self.base_unresolved_name()

    # baseUnresolvedName parses:
    #
    #	<base-unresolved-name> ::= <simple-id>
    #	                       ::= on <operator-name>
    #	                       ::= on <operator-name> <template-args>
    #	                       ::= dn <destructor-name>
    #
    #	<simple-id> ::= <source-name> [ <template-args> ]
    def base_unresolved_name(self) -> AST:
        s = self.s
        if s.startswith("on", self.pos) and self.n - self.pos >= 2:
            self.advance(2)
            n, _ = self.operator_name(True)
        elif s.startswith("dn", self.pos) and self.n - self.pos >= 2:
            self.advance(2)
            if is_digit(s[self.pos]):
                n = self.source_name()
            else:
                n = self.demangle_type(False)
            n = Destructor(n)
        elif is_digit(s[self.pos]):
            n = self.source_name()
        else:
            # GCC seems to not follow the ABI here: it can have
            # an operator name without on.
            # See https://gcc.gnu.org/PR70182.
            n, _ = self.operator_name(True)
        if s[self.pos] == "I":
            n = self.template(n)
        return n

    # requiresExpr parses:
    #
    #	<expression> ::= rQ <bare-function-type> _ <requirement>+ E
    #	             ::= rq <requirement>+ E
    #	<requirement> ::= X <expression> [N] [R <type-constraint>]
    #	              ::= T <type>
    #	              ::= Q <constraint-expression>
    def requires_expr(self) -> AST:
        s = self.s
        self.check_char("r")
        if self.pos >= self.n or (s[self.pos] != "q" and s[self.pos] != "Q"):
            self.fail("expected q or Q in requires clause in expression")
        kind = s[self.pos]
        self.advance(1)

        params = []
        if kind == "Q":
            while self.pos < self.n and s[self.pos] != "_":
                typ = self.demangle_type(False)
                params.append(typ)
            self.advance(1)

        requirements = []
        while self.pos < self.n and s[self.pos] != "E":
            c = s[self.pos]
            if c == "X":
                self.advance(1)
                expr = self.expression()
                noexcept = False
                if s[self.pos] == "N":
                    self.advance(1)
                    noexcept = True
                type_req = None
                if s[self.pos] == "R":
                    self.advance(1)
                    type_req, _ = self.name()
                req = ExprRequirement(expr, noexcept, type_req)

            elif c == "T":
                self.advance(1)
                typ = self.demangle_type(False)
                req = TypeRequirement(typ)

            elif c == "Q":
                self.advance(1)
                # We parse a regular expression rather than a
                # constraint expression.
                expr = self.expression()
                req = NestedRequirement(expr)

            else:
                self.fail("unrecognized requirement code")

            requirements.append(req)

        if s[self.pos] != "E":
            self.fail("expected E after requirements")
        self.advance(1)

        return RequiresExpr(params, requirements)

    # exprPrimary parses:
    #
    #	<expr-primary> ::= L <type> <(value) number> E
    #	               ::= L <type> <(value) float> E
    #	               ::= L <mangled-name> E
    def expr_primary(self) -> AST:
        s = self.s
        self.check_char("L")
        if self.pos >= self.n:
            self.fail("expected primary expression")

        # Check for 'Z' here because g++ incorrectly omitted the
        # underscore until -fabi-version=3.
        c = s[self.pos]
        if c == "_" or c == "Z":
            if c == "_":
                self.advance(1)
            if s[self.pos] != "Z":
                self.fail("expected mangled name")
            self.advance(1)
            ret = self.encoding(NOT_FOR_LOCAL_NAME)
        else:
            t = self.demangle_type(False)

            neg = False
            if s[self.pos] == "n":
                neg = True
                self.advance(1)
            if s[self.pos] == "E":
                if isinstance(t, BuiltinType) and t.name == "decltype(nullptr)":
                    # A nullptr should not have a value.
                    # We accept one if present because GCC
                    # used to generate one.
                    # https://gcc.gnu.org/PR91979.
                    pass
                elif isinstance(t, Closure):
                    # A closure doesn't have a value.
                    self.advance(1)
                    return LambdaExpr(t)
                elif _is_array_type(t):
                    self.advance(1)
                    return StringLiteral(t)
                else:
                    self.fail("missing literal value")
            i = s.find("E", self.pos, self.n)
            if i < 0:
                i = self.n
            val = s[self.pos : i]
            self.advance(i - self.pos)
            ret = Literal(t, val, neg)
        if s[self.pos] != "E":
            self.fail("expected E after literal")
        self.advance(1)
        return ret

    # discriminator parses:
    #
    #	<discriminator> ::= _ <(non-negative) number> (when number < 10)
    #	                    __ <(non-negative) number> _ (when number >= 10)
    def discriminator(self, a: AST) -> AST:
        s = self.s
        if s[self.pos] != "_":
            # clang can generate a discriminator at the end of
            # the string with no underscore.
            for i in range(self.pos, self.n):
                if not is_digit(s[i]):
                    return a
            # Skip the trailing digits.
            self.advance(self.n - self.pos)
            return a
        off = self.pos
        self.advance(1)
        trailing_underscore = False
        if s[self.pos] == "_":
            self.advance(1)
            trailing_underscore = True
        d = self.number()
        if d < 0:
            self.fail_earlier("invalid negative discriminator", self.pos - off)
        if trailing_underscore and d >= 10:
            if s[self.pos] != "_":
                self.fail("expected _ after discriminator >= 10")
            self.advance(1)
        # We don't currently print out the discriminator, so we don't
        # save it.
        return a

    # closureTypeName parses:
    #
    #	<closure-type-name> ::= Ul <lambda-sig> E [ <nonnegative number> ] _
    #	<lambda-sig> ::= <parameter type>+
    def closure_type_name(self) -> AST:
        self.check_char("U")
        self.check_char("l")

        old_lambda_template_level = self.lambda_template_level
        self.lambda_template_level = len(self.templates) + 1

        s = self.s
        template_args = []
        template = None
        while self.n - self.pos > 1 and s[self.pos] == "T":
            arg, template_val = self.template_param_decl()
            if arg is None:
                break
            template_args.append(arg)
            if template is None:
                template = Template(Name("lambda"), [])
                self.templates.append(template)
            template.args.append(template_val)

        template_args_constraint = None
        if s[self.pos] == "Q":
            template_args_constraint = self.constraint_expr()

        types = self.parmlist(False)

        self.lambda_template_level = old_lambda_template_level

        if template is not None:
            self.templates.pop()

        call_constraint = None
        if s[self.pos] == "Q":
            call_constraint = self.constraint_expr()

        if s[self.pos] != "E":
            self.fail("expected E after closure type name")
        self.advance(1)
        num = self.compact_number()
        return Closure(template_args, template_args_constraint, types, num, call_constraint)

    # templateParamDecl parses:
    #
    #	<template-param-decl> ::= Ty                          # type parameter
    #	                      ::= Tk <concept name> [<template-args>] # constrained type parameter
    #	                      ::= Tn <type>                   # non-type parameter
    #	                      ::= Tt <template-param-decl>* E # template parameter
    #	                      ::= Tp <template-param-decl>    # parameter pack
    #
    # Returns the new AST to include in the AST we are building and the
    # new AST to add to the list of template parameters.
    #
    # Returns None, None if not looking at a template-param-decl.
    def template_param_decl(self) -> Tuple[Optional[AST], Optional[AST]]:
        s = self.s
        if self.n - self.pos < 2 or s[self.pos] != "T":
            return None, None
        c = s[self.pos + 1]
        if c == "y":
            self.advance(2)
            name = TemplateParamName("$T", self.type_template_param_count)
            self.type_template_param_count += 1
            tp = TypeTemplateParam(name)
            return tp, name
        elif c == "k":
            # We don't track enclosing template parameter levels.
            # Don't try to demangle template parameter substitutions
            # in constraints.
            hold = self.parsing_constraint
            self.parsing_constraint = True
            try:
                self.advance(2)
                constraint, _ = self.name()
                name = TemplateParamName("$T", self.type_template_param_count)
                self.type_template_param_count += 1
                tp = ConstrainedTypeTemplateParam(name, constraint)
                return tp, name
            finally:
                self.parsing_constraint = hold
        elif c == "n":
            self.advance(2)
            name = TemplateParamName("$N", self.non_type_template_param_count)
            self.non_type_template_param_count += 1
            typ = self.demangle_type(False)
            tp = NonTypeTemplateParam(name, typ)
            return tp, name
        elif c == "t":
            self.advance(2)
            name = TemplateParamName("$TT", self.template_template_param_count)
            self.template_template_param_count += 1
            params = []
            template = None
            constraint = None
            while True:
                if self.pos >= self.n:
                    self.fail("expected closure template parameter")
                if s[self.pos] == "E":
                    self.advance(1)
                    break
                off = self.pos
                param, template_val = self.template_param_decl()
                if param is None:
                    self.fail_earlier("expected closure template parameter", self.pos - off)
                params.append(param)
                if template is None:
                    template = Template(Name("template_template"), [])
                    self.templates.append(template)
                template.args.append(template_val)

                if s[self.pos] == "Q":
                    # A list of template template
                    # parameters can have a constraint.
                    constraint = self.constraint_expr()
                    if s[self.pos] != "E":
                        self.fail("expected end of template template parameters after constraint")
            if template is not None:
                self.templates.pop()
            tp = TemplateTemplateParam(name, params, constraint)
            return tp, name
        elif c == "p":
            self.advance(2)
            off = self.pos
            param, template_val = self.template_param_decl()
            if param is None:
                self.fail_earlier("expected lambda template parameter", self.pos - off)
            return TemplateParamPack(param), template_val
        else:
            return None, None

    # unnamedTypeName parses:
    #
    #	<unnamed-type-name> ::= Ut [ <nonnegative number> ] _
    def unnamed_type_name(self) -> AST:
        self.check_char("U")
        self.check_char("t")
        num = self.compact_number()
        ret = UnnamedType(num)
        self.subs.append(ret)
        return ret

    def constraint_expr(self) -> AST:
        """Parse a constraint expression. This is just a regular
        expression, but template parameters are handled specially."""
        self.check_char("Q")

        hold = self.parsing_constraint
        self.parsing_constraint = True
        try:
            return self.expression()
        finally:
            self.parsing_constraint = hold

    def clone_suffix(self, a: AST) -> AST:
        """Recognize a clone suffix.  These are not part of the mangling
        API, but are added by GCC when cloning functions."""
        s = self.s
        p = self.pos
        n = self.n
        i = 0
        if n - p > 1 and s[p] == "." and (is_lower(s[p + 1]) or is_digit(s[p + 1]) or s[p + 1] == "_"):
            i += 2
            while n - p > i and (is_lower(s[p + i]) or is_digit(s[p + i]) or s[p + i] == "_"):
                i += 1
        while n - p > i + 1 and s[p + i] == "." and is_digit(s[p + i + 1]):
            i += 2
            while n - p > i and is_digit(s[p + i]):
                i += 1
        suffix = s[p : p + i]
        self.advance(i)
        return Clone(a, suffix)

    # substitution parses:
    #
    #	<substitution> ::= S <seq-id> _
    #	               ::= S_
    #	               ::= St
    #	               ::= Sa
    #	               ::= Sb
    #	               ::= Ss
    #	               ::= Si
    #	               ::= So
    #	               ::= Sd
    def substitution(self, for_prefix: bool) -> AST:
        self.check_char("S")
        if self.pos >= self.n:
            self.fail("missing substitution index")
        c = self.s[self.pos]
        off = self.pos
        if c == "_" or is_digit(c) or is_upper(c):
            id_ = self.seq_id(False)
            if id_ >= len(self.subs):
                self.fail_earlier(
                    "substitution index out of range (%d >= %d)" % (id_, len(self.subs)),
                    self.pos - off,
                )

            ret = self.subs[id_]

            # We need to update any references to template
            # parameters to refer to the currently active
            # template.

            # When copying a Typed we may need to adjust
            # the templates.
            copy_templates = list(self.templates)
            old_lambda_template_level: List[int] = []

            # push_template is called from skip, pop_template from copy.
            def push_template(template):
                copy_templates.append(template)
                old_lambda_template_level.append(self.lambda_template_level)
                self.lambda_template_level = 0

            def pop_template():
                copy_templates.pop()
                self.lambda_template_level = old_lambda_template_level.pop()

            def copy(a):
                if isinstance(a, Typed):
                    # Remove the template added in skip.
                    if isinstance(a.name, Template):
                        pop_template()
                    return None
                elif isinstance(a, Closure):
                    # Undo the save in skip.
                    self.lambda_template_level = old_lambda_template_level.pop()
                    return None
                elif isinstance(a, (TemplateParam, LambdaAuto)):
                    # A lambda auto parameter is represented
                    # as a template parameter, so we may have
                    # to change back when substituting.
                    index = a.index
                elif isinstance(a, PackExpansion):
                    # Find the possibly-new argument pack.
                    pack = self.find_argument_pack(a.base)
                    if pack is not a.pack:
                        return PackExpansion(a.base, pack)
                    return None
                else:
                    return None
                if self.parsing_constraint:
                    # We don't try to substitute template
                    # parameters in a constraint expression.
                    return Name("T%d" % index)
                if self.lambda_template_level > 0:
                    if isinstance(a, LambdaAuto):
                        return None
                    return LambdaAuto(index)
                template = None
                if copy_templates:
                    template = copy_templates[-1]
                elif isinstance(ret, Template):
                    # At least with clang we can see a template
                    # to start, and sometimes we need to refer
                    # to it. There is probably something wrong
                    # here.
                    template = ret
                else:
                    self.fail_earlier(
                        "substituted template parameter not in scope of template",
                        self.pos - off,
                    )
                if template is None:
                    # This template parameter is within
                    # the scope of a cast operator.
                    return TemplateParam(index, None)

                if index >= len(template.args):
                    self.fail_earlier(
                        "substituted template index out of range (%d >= %d)" % (index, len(template.args)),
                        self.pos - off,
                    )

                return TemplateParam(index, template)

            seen = set()

            def skip(a):
                if isinstance(a, Typed):
                    if isinstance(a.name, Template):
                        # This template is removed in copy.
                        push_template(a.name)
                    return False
                elif isinstance(a, Closure):
                    # This is undone in copy.
                    old_lambda_template_level.append(self.lambda_template_level)
                    self.lambda_template_level = len(copy_templates) + 1
                    return False
                elif isinstance(a, (TemplateParam, LambdaAuto)):
                    return False
                if a in seen:
                    return True
                seen.add(a)
                return False

            c = ret.copy(copy, skip)
            if c is not None:
                return c

            return ret
        else:
            self.advance(1)
            m = SUB_AST
            # For compatibility with the standard demangler, use
            # a longer name for a constructor or destructor.
            if for_prefix and (self.s[self.pos] == "C" or self.s[self.pos] == "D"):
                m = VERBOSE_AST
            a = m.get(c)
            if a is None:
                self.fail_earlier("unrecognized substitution code", 1)

            if self.s[self.pos] == "B":
                a = self.tagged_name(a)
                self.subs.append(a)

            return a

    def find_argument_pack(self, a: AST) -> Optional[ArgumentPack]:
        """Walk the AST looking for the argument pack for a pack
        expansion.  We find it via a template parameter."""
        seen = set()
        ret = []

        def fn(a):
            if ret:
                return False
            if isinstance(a, TemplateParam):
                if a.template is None or a.index >= len(a.template.args):
                    return True
                pack = a.template.args[a.index]
                if isinstance(pack, ArgumentPack):
                    ret.append(pack)
                    return False
            elif isinstance(
                a,
                (
                    PackExpansion,
                    Closure,
                    Name,
                    TaggedName,
                    Operator,
                    BuiltinType,
                    FunctionParam,
                    UnnamedType,
                    FixedType,
                    DefaultArg,
                ),
            ):
                return False
            if a in seen:
                return False
            seen.add(a)
            return True

        a.traverse(fn)
        return ret[0] if ret else None


def _is_array_type(typ: AST) -> bool:
    if isinstance(typ, TypeWithQualifiers):
        typ = typ.base
    return isinstance(typ, ArrayType)


def _find_template(check: AST) -> Optional[Template]:
    """findTemplate in encoding."""
    if isinstance(check, Template):
        return check
    elif isinstance(check, Qualified):
        if check.local_name:
            return _find_template(check.name)
        elif isinstance(check.name, Constructor):
            return _find_template(check.name)
    elif isinstance(check, MethodWithQualifiers):
        return _find_template(check.method)
    elif isinstance(check, Constructor):
        if check.base is not None:
            return _find_template(check.base)
    elif isinstance(check, Constraint):
        return _find_template(check.name)
    return None


def has_return_type(a: AST) -> bool:
    """Return whether the mangled form of a will have a return type."""
    if isinstance(a, Qualified):
        if a.local_name:
            return has_return_type(a.name)
        return False
    elif isinstance(a, Template):
        return not is_cdtor_conversion(a.name)
    elif isinstance(a, TypeWithQualifiers):
        return has_return_type(a.base)
    elif isinstance(a, MethodWithQualifiers):
        return has_return_type(a.method)
    elif isinstance(a, Constraint):
        return has_return_type(a.name)
    return False


def is_cdtor_conversion(a: AST) -> bool:
    """Report when an AST is a constructor, a destructor, or a
    conversion operator."""
    if isinstance(a, Qualified):
        return is_cdtor_conversion(a.name)
    return isinstance(a, (Constructor, Destructor, Cast))


def merge_qualifiers(q1_ast: Optional[AST], q2_ast: Optional[AST]) -> Optional[AST]:
    """Merge two qualifier lists into one."""
    if q1_ast is None:
        return q2_ast
    if q2_ast is None:
        return q1_ast
    if not isinstance(q1_ast, Qualifiers) or not isinstance(q2_ast, Qualifiers):
        raise InternalError("interface conversion")
    q1 = q1_ast
    m = set()
    for qual in q1.qualifiers:
        if not isinstance(qual, Qualifier):
            raise InternalError("interface conversion")
        if not qual.exprs:
            m.add(qual.name)
    rq = list(q1.qualifiers)
    for qual in q2_ast.qualifiers:
        if not isinstance(qual, Qualifier):
            raise InternalError("interface conversion")
        if qual.exprs:
            rq.append(qual)
        elif qual.name not in m:
            rq.append(qual)
            m.add(qual.name)
    q1.qualifiers = rq
    return q1


def simplify(a: AST) -> AST:
    """Replace template parameters with their expansions, and merge
    qualifiers."""
    seen = set()

    def skip(a):
        if a in seen:
            return True
        seen.add(a)
        return False

    r = a.copy(simplify_one, skip)
    if r is not None:
        return r
    return a


# The node types simplify_one may change (a fast pre-check; node classes
# are never subclassed, so this is equivalent to the isinstance chain).
_SIMPLIFY_TYPES = frozenset(
    {
        TemplateParam,
        MethodWithQualifiers,
        TypeWithQualifiers,
        ReferenceType,
        RvalueReferenceType,
        ArrayType,
        PackExpansion,
    }
)


def simplify_one(a: AST) -> Optional[AST]:
    """Simplify a single AST.  Return None if there is nothing to do."""
    if type(a) not in _SIMPLIFY_TYPES:
        return None
    if isinstance(a, TemplateParam):
        if a.template is not None and a.index < len(a.template.args):
            return a.template.args[a.index]
    elif isinstance(a, MethodWithQualifiers):
        m = a.method
        if isinstance(m, MethodWithQualifiers):
            ref = a.ref_qualifier
            if ref == "":
                ref = m.ref_qualifier
            elif m.ref_qualifier != "":
                if ref == "&" or m.ref_qualifier == "&":
                    ref = "&"
            return MethodWithQualifiers(m.method, merge_qualifiers(a.qualifiers, m.qualifiers), ref)
        if isinstance(m, TypeWithQualifiers):
            return MethodWithQualifiers(m.base, merge_qualifiers(a.qualifiers, m.qualifiers), a.ref_qualifier)
    elif isinstance(a, TypeWithQualifiers):
        b = a.base
        if isinstance(b, FunctionType):
            return MethodWithQualifiers(b, a.qualifiers, "")
        if isinstance(b, TypeWithQualifiers):
            return TypeWithQualifiers(b.base, merge_qualifiers(a.qualifiers, b.qualifiers))
        if isinstance(b, MethodWithQualifiers):
            return MethodWithQualifiers(b.method, merge_qualifiers(a.qualifiers, b.qualifiers), b.ref_qualifier)
    elif isinstance(a, ReferenceType):
        b = a.base
        if isinstance(b, ReferenceType):
            return b
        if isinstance(b, RvalueReferenceType):
            return ReferenceType(b.base)
    elif isinstance(a, RvalueReferenceType):
        b = a.base
        if isinstance(b, RvalueReferenceType):
            return b
        if isinstance(b, ReferenceType):
            return b
    elif isinstance(a, ArrayType):
        # Qualifiers on the element of an array type
        # go on the whole array type.
        q = a.element
        if isinstance(q, TypeWithQualifiers):
            return TypeWithQualifiers(ArrayType(a.dimension, q.base), q.qualifiers)
    elif isinstance(a, PackExpansion):
        # Expand the pack and replace it with a list of
        # expressions.
        if a.pack is not None:
            exprs = []
            pack = a.pack
            for arg in pack.args:

                def copy(sub, arg=arg):
                    # Replace the ArgumentPack
                    # with a specific argument.
                    if sub is pack:
                        return arg
                    # Copy everything else.
                    return None

                seen = set()

                def skip(sub, seen=seen):
                    # Don't traverse into another
                    # pack expansion.
                    if isinstance(sub, PackExpansion):
                        return True
                    if sub in seen:
                        return True
                    seen.add(sub)
                    return False

                b = a.base.copy(copy, skip)
                if b is None:
                    b = a.base
                exprs.append(simplify(b))
            return ExprList(exprs)
    return None
