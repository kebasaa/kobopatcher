"""Tests for kobopatcher.patchlib.css (port of kobopatch IsCSS + riking/cssparse tokenizer)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from kobopatcher.patchlib.css import (
    EOF,
    Token,
    TokenExtraError,
    TokenExtraHash,
    TokenExtraNumeric,
    TokenExtraUnicodeRange,
    Tokenizer,
    TokenType,
    is_css,
    normalize,
)

T = TokenType
FIXTURE = Path(__file__).parent / "testdata" / "css_iscss_cases.json"


# ---------------------------------------------------------------------------
# kobopatch patchlib/css_test.go: TestIsCSS

IS_CSS_CASES = [
    ("", False, "no rules are present"),
    ("asd", False, "no rules are present"),
    (" asd", False, "no rules are present"),
    ("asd {", False, "there are incomplete rules"),
    ("asd {}", False, "there aren't any properties (technically it is CSS, but for all intents and purposes it isn't)"),
    ("asd {asd:fgh}", True, "it obviously is"),
    (" asd {asd: fgh}", True, "it obviously is"),
    (" asd {asd: fgh}/*}*/", True, "it obviously is"),
    ("    * {asd: fgh}/*}*/", True, "it obviously is"),
    (" asd {asd: url(:sdf{}dfg)}", True, "it obviously is"),
    (" asd {asd: fgh; dfg: asd}", True, "it obviously is"),
    (" @media print { asd {asd: fgh; dfg: asd} }", True, "it obviously is"),
    ("[prop]{dfg: asd}", True, "it obviously is"),
    (".class{dfg: asd}", True, "it obviously is"),
    ("#id{dfg: asd}", True, "it obviously is"),
    (":hover{dfg: asd}", True, "it obviously is"),
    (":hover{dfg: asd} fgh {content: 'dfg}'}", True, "it obviously is"),
    (":hover{dfg: asd; fgh: asd;} fgh {dfgdfgdfg}", True, "it's syntactically valid"),
    ("function asd() { fgh(); }", False, "there's more blocks than rules (i.e. it's JavaScript)"),
    ("<html><span style='asd: dfg'>dfgdfg</span></html>", False, "it obviously isn't (i.e. it's HTML)"),
    (
        "<html><span style='asd: dfg'>dfgdfg</span><style>dfg {asd: dfg} fgh {dfg: ert; fgh: asd}</style></html>",
        False,
        "it obviously isn't (i.e. it's HTML, even though it might contain CSS in it), as it doesn't start with a selector",
    ),
]


def test_is_css_case_count():
    assert len(IS_CSS_CASES) == 21


@pytest.mark.parametrize("inp,out,why", IS_CSS_CASES)
def test_is_css(inp, out, why):
    assert is_css(inp) is out, f"{inp!r} should {'' if out else 'not '}be CSS because {why}"
    assert is_css(inp.encode("utf-8")) is out


# ---------------------------------------------------------------------------
# Behaviour of the Go original that is easy to get wrong.


@pytest.mark.parametrize(
    "inp,out",
    [
        ("/*c*/a{b:c}", False),  # comments are tokens, so this does not start with a selector
        ("a{b/*c*/:c}", False),  # comment between ident and colon: not a property
        ("a{b :c}", False),  # whitespace between ident and colon: not a property
        ("a{b:c}x", True),  # trailing ident is dropped at EOF (not counted either way)
        ("a{b:c", False),
        ("a{b:c}{", False),
        ("a{b:c}}", False),
        ("a{b:c}/*", True),  # unterminated comment runs to EOF and is dropped
        ("\x00a{b:c}", True),  # NUL -> U+FFFD, a name-start code point (ident)
        ("\fa{b:c}", False),  # form feed is not normalised and not whitespace -> DELIM
        ("a{b\f:c}", False),
        ("\r\na{b:c}", True),
        ("*{b:c}", True),
        ("*={b:c}", False),  # SUBSTRING-MATCH, not DELIM "*"
        (".a{b:c}", True),
        (".5{b:c}", False),  # NUMBER
        ("u+1{b:c}", False),  # UNICODE-RANGE
        ("a{b:c;d:e}f{}", True),
        ("a{b:c}f{}", False),
        ("a:b{c:d}", True),
    ],
)
def test_is_css_subtleties(inp, out):
    assert is_css(inp) is out


def test_eof_drops_last_token():
    tk = Tokenizer("ab")
    t = tk.next()
    # consumeName had to read past 'b' and hit EOF, so err is already set
    assert t == Token(T.IDENT, "ab")
    assert tk.err() is EOF
    assert tk.next().type == T.EOF


def test_whitespace_does_not_set_eof():
    tk = Tokenizer("} \n ")
    assert tk.next().type == T.CLOSE_BRACE and tk.err() is None
    assert tk.next() == Token(T.S, "\n") and tk.err() is None
    assert tk.next().type == T.EOF and tk.err() is EOF


def test_normalize():
    assert normalize(b"a\r\nb\rc\r\r\nd\n\x00\f") == b"a\nb\nc\n\nd\n\xef\xbf\xbd\f"


def test_input_types():
    import io

    s = "é{ü:ö}"
    assert is_css(s) and is_css(s.encode()) and is_css(bytearray(s.encode())) and is_css(io.BytesIO(s.encode()))


# ---------------------------------------------------------------------------
# riking/cssparse tokenizer/scanner_test.go: TestMatchers (the Fuzz()
# round-trip it also runs needs the token renderer, which is not ported).

# Go TokenExtraTypeLookup: types whose expected entry carries an Extra.
_EXTRA_TYPES = {
    T.ERROR,
    T.BAD_ESCAPE,
    T.BAD_STRING,
    T.BAD_URI,
    T.HASH,
    T.NUMBER,
    T.PERCENTAGE,
    T.DIMENSION,
    T.UNICODE_RANGE,
}


def _gostr(v) -> str:
    """Go string literal (str or bytes) -> our surrogateescape str repr."""
    if isinstance(v, bytes):
        return v.decode("utf-8", "surrogateescape")
    return v


def check_match(s, *tt_list):
    tz = Tokenizer(s.encode("utf-8", "surrogateescape") if isinstance(s, str) else s)
    i = 0
    while i < len(tt_list):
        tt = tt_list[i]
        t_val = _gostr(tt_list[i + 1])
        t_extra = tt_list[i + 2] if tt in _EXTRA_TYPES else None
        tok = tz.next()
        assert tok.type == tt, f"did not match: {s!r} (got {tok}, wanted {tt})"
        assert tok.value == t_val, f"did not match: {s!r} (got {tok.value!r}, wanted {t_val!r})"
        if t_extra is not None and tok.extra != t_extra:
            assert tt.stop_token() and tt not in (T.ERROR, T.EOF), (
                f"did not match .Extra: {s!r} (got {tok.extra!r}, wanted {t_extra!r})"
            )
        i += 3 if tt in _EXTRA_TYPES else 2
    tok = tz.next()
    assert tok.type == T.EOF, f"missing EOF after token {s!r}, got {tok}"


N = TokenExtraNumeric
H = TokenExtraHash
U = TokenExtraUnicodeRange
E = TokenExtraError


MATCHERS = [
    ("abcd", T.IDENT, "abcd"),
    ('"abcd"', T.STRING, "abcd"),
    ('"ab\'cd"', T.STRING, "ab'cd"),
    (r'"ab\"cd"', T.STRING, 'ab"cd'),
    (r'"ab\\cd"', T.STRING, r"ab\cd"),
    ("'abcd'", T.STRING, "abcd"),
    ("'ab\"cd'", T.STRING, 'ab"cd'),
    (r"'ab\'cd'", T.STRING, "ab'cd"),
    (r"'ab\\cd'", T.STRING, r"ab\cd"),
    ("#name", T.HASH, "name", H(is_identifier=True)),
    ("##name", T.DELIM, "#", T.HASH, "name", H(is_identifier=True)),
    ("#123", T.HASH, "123", H(is_identifier=False)),
    ("42''", T.NUMBER, "42", N(), T.STRING, ""),
    ("+42", T.NUMBER, "+42", N()),
    ("-42", T.NUMBER, "-42", N()),
    ("42.", T.NUMBER, "42", N(), T.DELIM, "."),
    ("42.0", T.NUMBER, "42.0", N(non_integer=True)),
    ("4.2", T.NUMBER, "4.2", N(non_integer=True)),
    (".42", T.NUMBER, ".42", N(non_integer=True)),
    ("+.42", T.NUMBER, "+.42", N(non_integer=True)),
    ("-.42", T.NUMBER, "-.42", N(non_integer=True)),
    ("42%", T.PERCENTAGE, "42", N()),
    ("4.2%", T.PERCENTAGE, "4.2", N(non_integer=True)),
    (".42%", T.PERCENTAGE, ".42", N(non_integer=True)),
    ("42px", T.DIMENSION, "42", N(dimension="px")),
    ("5e", T.DIMENSION, "5", N(dimension="e")),
    ("5e-", T.DIMENSION, "5", N(dimension="e-")),
    ("5e-3", T.NUMBER, "5e-3", N(non_integer=True)),
    (b"5e-\xf1", T.DIMENSION, "5", N(dimension=_gostr(b"e-\xf1"))),
    ("url(http://domain.com)", T.URI, "http://domain.com"),
    ("url( http://domain.com/uri/between/space )", T.URI, "http://domain.com/uri/between/space"),
    ("url('http://domain.com/uri/between/single/quote')", T.URI, "http://domain.com/uri/between/single/quote"),
    ('url("http://domain.com/uri/between/double/quote")', T.URI, "http://domain.com/uri/between/double/quote"),
    ("url(http://domain.com/?parentheses=%28)", T.URI, "http://domain.com/?parentheses=%28"),
    (
        "url( http://domain.com/?parentheses=%28&between=space )",
        T.URI,
        "http://domain.com/?parentheses=%28&between=space",
    ),
    (
        "url('http://domain.com/uri/(parentheses)/between/single/quote')",
        T.URI,
        "http://domain.com/uri/(parentheses)/between/single/quote",
    ),
    (
        'url("http://domain.com/uri/(parentheses)/between/double/quote")',
        T.URI,
        "http://domain.com/uri/(parentheses)/between/double/quote",
    ),
    (
        r"url(http://domain.com/uri/\(bare%20escaped\)/parentheses)",
        T.URI,
        "http://domain.com/uri/(bare%20escaped)/parentheses",
    ),
    (
        "url(http://domain.com/uri/1)url(http://domain.com/uri/2)",
        T.URI,
        "http://domain.com/uri/1",
        T.URI,
        "http://domain.com/uri/2",
    ),
    (
        "url(http://domain.com/uri/1) url(http://domain.com/uri/2)",
        T.URI,
        "http://domain.com/uri/1",
        T.S,
        " ",
        T.URI,
        "http://domain.com/uri/2",
    ),
    ("U+0042", T.UNICODE_RANGE, "U+0042", U(start=0x42, end=0x42)),
    ("U+FFFFFF", T.UNICODE_RANGE, "U+FFFFFF", U(start=0xFFFFFF, end=0xFFFFFF)),
    ("U+??????", T.UNICODE_RANGE, "U+0000-FFFFFF", U(start=0, end=0xFFFFFF)),
    ("<!--", T.CDO, "<!--"),
    ("-->", T.CDC, "-->"),
    ("   \n   \t   \n", T.S, "\n"),
    ("/**/", T.COMMENT, ""),
    ("/***/", T.COMMENT, "*"),
    ("/**", T.COMMENT, "*"),
    ("/*foo*/", T.COMMENT, "foo"),
    ("/* foo */", T.COMMENT, " foo "),
    ("bar(", T.FUNCTION, "bar"),
    ("~=", T.INCLUDES, "~="),
    ("|=", T.DASH_MATCH, "|="),
    ("||", T.COLUMN, "||"),
    ("^=", T.PREFIX_MATCH, "^="),
    ("$=", T.SUFFIX_MATCH, "$="),
    ("*=", T.SUBSTRING_MATCH, "*="),
    ("{", T.OPEN_BRACE, "{"),
    ('╯︵┻━┻"stuff"', T.IDENT, "╯︵┻━┻", T.STRING, "stuff"),
    (
        "foo { bar: rgb(255, 0, 127); }",
        T.IDENT, "foo", T.S, " ",
        T.OPEN_BRACE, "{", T.S, " ",
        T.IDENT, "bar", T.COLON, ":", T.S, " ",
        T.FUNCTION, "rgb",
        T.NUMBER, "255", N(), T.COMMA, ",", T.S, " ",
        T.NUMBER, "0", N(), T.COMMA, ",", T.S, " ",
        T.NUMBER, "127", N(), T.CLOSE_PAREN, ")",
        T.SEMICOLON, ";", T.S, " ",
        T.CLOSE_BRACE, "}",
    ),  # fmt: skip
    # Fuzzing results
    ("ur(0", T.FUNCTION, "ur", T.NUMBER, "0", N()),
    ("1\\15", T.DIMENSION, "1", N(dimension="\x15")),
    ("url(0t')", T.BAD_URI, "0t", E()),
    ("uri/", T.IDENT, "uri", T.DELIM, "/"),
    ("\x00", T.IDENT, "�"),
    ("a\\0", T.IDENT, "a�"),
    ("b\\\\0", T.IDENT, "b\\0"),
    ("00\\d", T.DIMENSION, "00", N(dimension="\r")),
    # note: \f is form feed, which is 0x0C
    ("\\0\\0\\C\\\f\\\\0", T.IDENT, "��\x0c\x0c\\0"),
    # String running to EOF is success, not badstring
    ('"a0\\d', T.STRING, "a0\x0d"),
    ('"a0\r', T.BAD_STRING, "a0", E(), T.S, "\n"),
    ("\\fun(", T.FUNCTION, "\x0fun"),
    ('"abc\\"def\nghi', T.BAD_STRING, 'abc"def', E(), T.S, "\n", T.IDENT, "ghi"),
]


@pytest.mark.parametrize("case", MATCHERS, ids=lambda c: repr(c[0])[:40])
def test_matchers(case):
    check_match(*case)


def test_matchers_long_css_tokenizes():
    # The upstream test ends by fuzzing this document; at least check it
    # tokenizes without error tokens and is detected as CSS.
    css = (
        "#sw_tfbb,#id_d{display:none}.sw_pref{border-style:solid;border-width:7px 0 7px 10px;vertical-align:bottom}"
        "#b_tween{margin-top:-28px}#b_tween>span{line-height:30px}#b_tween .ftrH{line-height:30px;height:30px}"
        "input{font:inherit;font-size:100%}.b_searchboxForm{font:18px/normal 'Segoe UI',Arial,Helvetica,Sans-Serif}"
        ".b_beta{font:11px/normal Arial,Helvetica,Sans-Serif}.b_scopebar,.id_button{line-height:30px}"
        ".sa_ec{font:13px Arial,Helvetica,Sans-Serif}#sa_ul .sa_hd{font-size:11px;line-height:16px}"
        "#sw_as strong{font-family:'Segoe UI Semibold',Arial,Helvetica,Sans-Serif}"
        "#id_h{background-color:transparent!important;position:relativ e!important;float:right;height:35px!important;"
        "width:280px!important}.sw_pref{margin:0 15px 3px 0}#id_d{left:auto;right:26px;top:35px!important}"
        ".id_avatar{vertical-align:middle;margin:10px 0 10px 10px}"
    )
    toks = list(Tokenizer(css))
    assert toks and all(not t.type.stop_token() for t in toks)
    assert is_css(css)


# ---------------------------------------------------------------------------
# Differential fixture generated against Go by tools/parity/css_diff.py.


def _extra(t: Token) -> str:
    e = t.extra
    if isinstance(e, TokenExtraHash):
        return "h" + ("true" if e.is_identifier else "false")
    if isinstance(e, TokenExtraNumeric):
        return "n" + ("true" if e.non_integer else "false") + e.dimension.encode("utf-8", "surrogateescape").hex()
    if isinstance(e, TokenExtraUnicodeRange):
        return "u%x-%x" % (e.start, e.end)
    if isinstance(e, TokenExtraError):
        return "e"
    return ""


def _token_line(data: bytes) -> str:
    tk = Tokenizer(data)
    out = []
    while True:
        t = tk.next()
        out.append("%d:%s:%s " % (int(t.type), t.value.encode("utf-8", "surrogateescape").hex(), _extra(t)))
        if tk.err() is not None:
            out.append("|" + str(tk.err()))
            return "".join(out)


def _fixture_cases():
    doc = json.loads(FIXTURE.read_text())
    return doc["cases"]


def test_fixture_is_css_matches_go():
    cases = _fixture_cases()
    assert len(cases) > 1000
    assert any(c["is_css"] for c in cases) and not all(c["is_css"] for c in cases)
    bad = []
    for c in cases:
        data = base64.b64decode(c["in"])
        if is_css(data) is not c["is_css"]:
            bad.append((data, c["is_css"]))
    assert not bad, bad[:20]


def test_fixture_tokens_match_go():
    bad = []
    for c in _fixture_cases():
        data = base64.b64decode(c["in"])
        got = _token_line(data)
        if got != c["tokens"]:
            bad.append((data, c["tokens"], got))
    assert not bad, bad[:5]
