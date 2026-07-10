"""Typed brain for the teleport-harness.

The docker/nginx/cert/build *plumbing* stays in lib/*.sh; this package owns the
data + decision layer that used to be grep/sed/awk over YAML: module models,
feature/version gating, and (incrementally) verification, templating, reporting.
"""
