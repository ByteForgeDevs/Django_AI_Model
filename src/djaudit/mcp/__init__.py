"""Expose djaudit to coding agents over the Model Context Protocol.

The rest of djaudit answers "what is wrong with this project" for a human. This
package answers it for whatever is *writing* the project, which is increasingly
a language model, and which cannot see the defects it is producing.

That is not a guess. A frontier model asked for a routine Django app -- a
model, two serializers and three views, about forty lines -- produced two SQL
injections, an N+1, two ``fields = "__all__"`` serializers and a nullable
``CharField``, and reported itself finished. Every one of those is a rule here.
The model was not careless; the defects are simply invisible at the level a
model writes at, because ``order.customer.email`` is spelled identically
whether it costs one query or one per row.

So the loop this package closes is: the model writes, djaudit reads, the model
repairs. What makes it worth building rather than describing is that the middle
step is deterministic. A model reviewing its own output is the same faculty
that produced the defect; a rule is not.

The server speaks stdio JSON-RPC and adds no dependency to djaudit, which
matters more than the convenience of an SDK: this process is launched by an
agent inside a developer's editor, and the argument for pointing djaudit at
code you have not read is that it parses and never executes. A transport layer
is not worth weakening that.
"""

from djaudit.mcp.server import PROTOCOL_VERSION, TOOLS, serve

__all__ = ["PROTOCOL_VERSION", "TOOLS", "serve"]
