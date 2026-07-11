"""The declarative `checks:` vocabulary — the verbs usable in a module's
`checks:` block, with their argument arities.

This registry is the schema the `validate` command checks module `checks:` blocks
against. It mirrors the `assert_<verb>` functions in `lib/assert.sh`; when those
impls move to Python (phase 3) this becomes the single source of truth. Verbs
defined only in a module's `checks.sh` escape hatch are NOT here on purpose — the
declarative block runs *before* `checks.sh` is sourced, so it can only call
library verbs (see lib/verify.sh).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerbSpec:
    name: str
    min_args: int
    max_args: int | None  # None => variadic (unbounded)
    usage: str

    def arity_ok(self, n: int) -> bool:
        if n < self.min_args:
            return False
        return self.max_args is None or n <= self.max_args


# The declarative vocabulary. arity = number of args AFTER the verb. Every verb here
# must have an impl in harness/verify.py IMPLS (a test enforces both directions).
_SPECS: list[VerbSpec] = [
    VerbSpec("node_present", 1, 1, "node_present <suffix>"),
    VerbSpec("node_absent", 1, 1, "node_absent <suffix>"),
    VerbSpec("node_scope", 2, 2, "node_scope <suffix> <scope>"),
    VerbSpec("node_count", 1, 1, "node_count <n>"),
    VerbSpec("scoped_node_count", 2, 2, "scoped_node_count <scope> <n>"),
    VerbSpec("log_contains", 2, None, "log_contains <container-suffix> <regex...>"),
    VerbSpec("bot_joined", 1, 2, "bot_joined <bot-name> [join-method]"),
    VerbSpec("output_file", 2, 2, "output_file <container-suffix> <path>"),
    VerbSpec("no_output_file", 2, 2, "no_output_file <container-suffix> <path>"),
    VerbSpec(
        "identity_authorized", 2, 3,
        "identity_authorized <container-suffix> <identity-path> [auth-server]",
    ),
    VerbSpec(
        "identity_scope", 3, 3,
        "identity_scope <container-suffix> <identity-path> <scope>",
    ),
    VerbSpec("tsh_ssh", 1, 2, "tsh_ssh <suffix> [login]"),
    VerbSpec(
        "tsh_ssh_as", 3, 4,
        "tsh_ssh_as <container-suffix> <identity-path> <node-suffix> [login]",
    ),
]

REGISTRY: dict[str, VerbSpec] = {s.name: s for s in _SPECS}
