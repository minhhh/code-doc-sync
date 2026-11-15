"""Build pipeline: parse, post-process, and serialize the graph.

``incremental`` collects files and writes nodes and edges, ``postprocessing``
runs the derived stages (signatures, FTS, flows, communities), ``build``
decides between a full build and an update, and ``export`` projects the
finished artifact back out as JSON.
"""
