"""graph: the switch for Mission Control's Graph page (#167).

The page itself (/graph, /graph.json, /graph/related) is core's, like `orch graph`, because addons render widgets
only; core serves it only while this addon is enabled in the workspace. The addon runs nothing.
"""
from __future__ import annotations


class Graph:
    def __init__(self, ctx):
        self.ctx = ctx


def create(ctx):
    return Graph(ctx)
