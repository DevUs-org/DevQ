'''
Tags: Main

Tokenizer for OpenQASM 2.0.

Real lexing, not whitespace splitting. The original reader split lines
on spaces, so `cx q[0],q[1]` and `cx q[0], q[1]` tokenised differently
and a space inside a parameter expression (`rx(pi / 2)`) broke it. A
tokenizer removes whitespace as a concern entirely: tokens are produced
by kind, and the parser never sees layout.

Token kinds:
  NUMBER   integer or real literal, including scientific notation
  ID       identifier or keyword (the parser distinguishes keywords)
  STRING   a double-quoted string (only the include target uses one)
  SYMBOL   one of  ( ) [ ] { } , ;  and the arrow  ->
  OP       one of  + - * / ^
  EOF      end of input

Comments — // to end of line, and /* ... */ block — are consumed here
and never reach the parser. Line numbers are tracked on every token so
errors can point at a source line.
'''


class QASMError(Exception):
    '''
    A malformed-source error, carrying the 1-based source line.

    Raised by the tokenizer and parser alike; the frontend wraps the
    message with the file name. Kept distinct from Python errors so a
    genuine bug in the parser is not mistaken for bad input.
    '''
    def __init__(self, message, line=None):
        self.line = line
        if line is not None:
            super().__init__(f"line {line}: {message}")
        else:
            super().__init__(message)


class Token:
    __slots__ = ("kind", "value", "line")

    def __init__(self, kind, value, line):
        self.kind = kind
        self.value = value
        self.line = line

    def __repr__(self):
        return f"Token({self.kind}, {self.value!r}, line={self.line})"


# Single-character symbols. The two-character arrow -> is handled before
# these, since '-' alone is also an operator.
_SYMBOLS = set("()[]{},;")
_OPS = set("+-*/^")


def tokenize(text):
    '''
    Turn source text into a list of Tokens, terminated by an EOF token.

    Raises:
        QASMError: on an unterminated string or block comment, or a
                   character that cannot begin any token.
    '''
    tokens = []
    i = 0
    line = 1
    n = len(text)

    while i < n:
        c = text[i]

        # ── Newlines and whitespace ──────────────────────────────────
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue

        # ── Comments ─────────────────────────────────────────────────
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            # Line comment: skip to end of line (the newline is handled
            # next loop so the line counter stays correct).
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            start = line
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] == "\n":
                    line += 1
                i += 1
            if i >= n:
                raise QASMError("unterminated block comment", start)
            i += 2  # consume the closing */
            continue

        # ── Arrow (before '-' as an operator) ────────────────────────
        if c == "-" and i + 1 < n and text[i + 1] == ">":
            tokens.append(Token("SYMBOL", "->", line))
            i += 2
            continue

        # ── Equality (only appears in `if (creg == N)`) ──────────────
        # '=' is not an arithmetic operator, so it exists solely for the
        # conditional the parser rejects. Recognised here so that a file
        # using `if` fails with the parser's precise "no feedback"
        # message, not a cryptic "unexpected character '='" from the
        # lexer. A lone '=' (assignment, which 2.0 has no use for) is
        # likewise surfaced as a token the parser can complain about.
        if c == "=":
            if i + 1 < n and text[i + 1] == "=":
                tokens.append(Token("SYMBOL", "==", line))
                i += 2
            else:
                tokens.append(Token("SYMBOL", "=", line))
                i += 1
            continue

        # ── Strings (only the include target) ────────────────────────
        if c == '"':
            start = line
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\n":
                    line += 1
                buf.append(text[i])
                i += 1
            if i >= n:
                raise QASMError("unterminated string", start)
            i += 1  # consume closing quote
            tokens.append(Token("STRING", "".join(buf), line))
            continue

        # ── Numbers ──────────────────────────────────────────────────
        # A leading digit, or a leading dot followed by a digit (.5).
        if c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            seen_dot = False
            seen_exp = False
            while j < n:
                ch = text[j]
                if ch.isdigit():
                    j += 1
                elif ch == "." and not seen_dot and not seen_exp:
                    seen_dot = True
                    j += 1
                elif ch in "eE" and not seen_exp:
                    seen_exp = True
                    j += 1
                    # An optional sign may follow the exponent marker.
                    if j < n and text[j] in "+-":
                        j += 1
                else:
                    break
            tokens.append(Token("NUMBER", text[i:j], line))
            i = j
            continue

        # ── Identifiers and keywords ─────────────────────────────────
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(Token("ID", text[i:j], line))
            i = j
            continue

        # ── Operators ────────────────────────────────────────────────
        if c in _OPS:
            tokens.append(Token("OP", c, line))
            i += 1
            continue

        # ── Single-character symbols ─────────────────────────────────
        if c in _SYMBOLS:
            tokens.append(Token("SYMBOL", c, line))
            i += 1
            continue

        raise QASMError(f"unexpected character {c!r}", line)

    tokens.append(Token("EOF", None, line))
    return tokens