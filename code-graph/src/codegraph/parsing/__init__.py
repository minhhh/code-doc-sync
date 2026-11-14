"""Parsing and cross-file resolution.

``parser`` does the Tree-sitter extraction; the other modules fill gaps it
cannot see, either during the parse (tsconfig aliases, custom languages) or
as a post-pass over the store (receivers, DI wiring, framework calls).
"""
