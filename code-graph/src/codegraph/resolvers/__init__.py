"""Cross-file resolution passes that run over the store after parsing.

Each module exposes a ``resolve_*`` function that rewrites unresolved or
partially-resolved edges using evidence the parser could not see: injected
dependency types, framework wiring, module graphs, and declared receiver
types. They only ever turn a bare target into a qualified one, never guess.
"""
