"""Read-side engine: queries and analysis over a built graph.

Everything here takes a ``GraphStore`` and returns plain data. Nothing writes
to the database and nothing parses source; the build lives in ``build``,
``incremental`` and ``postprocessing``.
"""
