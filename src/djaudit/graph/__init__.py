"""The model graph: what the application's data model actually looks like.

Settings rules could get by reading one module at a time. Authorization rules
cannot. To know whether an endpoint leaks somebody else's data you first have
to know what the endpoint returns, which model that is, and whether rows of
that model belong to a user at all — and none of those questions can be
answered from the file the endpoint is written in.

So this package reconstructs the model graph from source: every model, its
fields, the relations between them, and the paths from any model to the user
model. It never imports the target, which means it works on a checkout with no
dependencies installed and never runs code from a repository we are auditing.
"""

from __future__ import annotations

from djaudit.graph.builder import build_model_graph
from djaudit.graph.nodes import ModelGraph, ModelNode

__all__ = ["ModelGraph", "ModelNode", "build_model_graph"]
