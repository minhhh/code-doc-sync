"""Tree-sitter extraction and the language config it needs.

``parser`` does the extraction, ``custom`` loads repo-defined languages, and
``tsconfig`` resolves TS path aliases during the parse. Passes that run over
the finished graph live in :mod:`codegraph.resolvers`.
"""
