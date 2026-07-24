'''
Tags: Main

Environment-variable placeholder resolution for workload specs.

A spec may reference an environment variable anywhere a string appears,
written ``${NAME}``. Resolution happens once, in load_spec, AFTER the
JSON is read and BEFORE validate_spec runs — so validation sees the
values that will actually run, not the placeholders.

WHY A SEPARATE PASS, ABOVE VALIDATION. The spec's strictness (unknown
keys, missing required fields, coercible scalars) is about the resolved
values. A placeholder is not yet a value; validating it would validate
the wrong thing. Resolving first means every downstream check — type
coercion included — operates on what the run will really use.

WHY THE LOG IS SAFE. This pass returns a NEW resolved spec. The caller
records the PRE-resolution spec verbatim in the log header, so a
resolved value never reaches disk: ``ibm.${PROVIDER_TYPE}`` is logged
as ``ibm.${PROVIDER_TYPE}``, and ``${IONQ_API_KEY}`` never appears
expanded anywhere. That verbatim-logging path is what keeps secrets out
of published artifacts — it is load-bearing, not a nicety. Do not add a
code path that logs the resolved spec.

THE GRAMMAR IS FIXED AND ENVIRONMENT-INDEPENDENT.
  - A placeholder is exactly ``${NAME}`` where NAME matches
    ``[A-Za-z_][A-Za-z0-9_]*``. Lookup is CASE-SENSITIVE and EXACT:
    ``${seed}`` reads ``seed``, ``${SEED}`` reads ``SEED``, and they
    are different variables. DevQ never recases or guesses.
  - A well-formed placeholder whose variable is unset is a hard
    SpecError — consistent with spec.py's refuse-rather-than-guess
    stance. A missing credential must fail at load, not surface as an
    auth failure three layers down.
  - Anything ``${...}``-shaped that does NOT match the grammar
    (``${}``, ``${1BAD}``, ``${with-dash}``) is NOT a placeholder. It
    is left untouched as a literal, because a spec may legitimately
    contain a ``$`` or a brace. Only the exact grammar triggers a
    lookup; nothing else is a "malformed placeholder" to error on.

SUBSTITUTION IS EMBEDDED AND REPEATED. ``${NAME}`` resolves wherever it
appears in a string, not only as the whole value, and every occurrence
is replaced: ``ibm.${VENDOR}.${TIER}`` becomes ``ibm.a.b``. The result
of any substitution is always a STRING — it has to be, since it can be
concatenated into surrounding text — and the field's own validation in
spec.py coerces scalars (seed, repeat, thresholds) afterwards. This
module is deliberately type-blind: an environment holds only strings,
so there is no type here to read.

SCOPE. Every string value in the spec is scanned, recursively, through
nested dicts and lists — device backends, credentials, job fields, all
of it. Dict KEYS are not touched; a placeholder in a key position would
change the spec's shape, and spec keys are a fixed vocabulary.
'''

import os
import re

from benchmark.spec import SpecError


# Exactly ${NAME}, NAME = an identifier. The surrounding ${...} is fixed
# text; only NAME is captured. Anything not matching this — ${}, ${1x},
# ${a-b}, a bare $FOO — is not a placeholder and is left as a literal.
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_string(value, where):
    '''
    Replace every ${NAME} in one string with os.environ[NAME].

    Case-sensitive, exact lookup. An unset variable is a hard
    SpecError naming the variable and the spec position. Text that only
    LOOKS like a placeholder but does not match the grammar is left
    untouched — the regex simply does not see it.
    '''
    missing = []

    def substitute(match):
        name = match.group(1)
        if name not in os.environ:
            missing.append(name)
            return match.group(0)  # unused — we raise below
        return os.environ[name]

    resolved = _PLACEHOLDER.sub(substitute, value)

    if missing:
        raise SpecError(
            f"{where}: environment variable(s) {sorted(set(missing))} "
            f"referenced by a ${{...}} placeholder are not set. A spec "
            f"placeholder must resolve at load time — an unset variable "
            f"is refused rather than guessed, so a missing credential or "
            f"seed fails here instead of three layers down. Lookup is "
            f"case-sensitive and exact."
        )
    return resolved


def _resolve(node, where):
    '''Recurse through the spec, resolving placeholders in every string.

    Dict keys are left alone — only values are scanned. Non-string,
    non-container leaves (ints, floats, bools, None) pass through
    untouched; a placeholder can only ever have been written as a
    string in JSON.
    '''
    if isinstance(node, str):
        return _resolve_string(node, where)
    if isinstance(node, dict):
        return {key: _resolve(val, f"{where}.{key}")
                for key, val in node.items()}
    if isinstance(node, list):
        return [_resolve(item, f"{where}[{i}]")
                for i, item in enumerate(node)]
    return node


def resolve_placeholders(spec, source="<spec>"):
    '''
    Return a new spec with every ${NAME} resolved from the environment.

    The input is not mutated — the caller keeps the original to record
    verbatim in the log header. Raises SpecError for any well-formed
    placeholder whose variable is unset.
    '''
    return _resolve(spec, source)