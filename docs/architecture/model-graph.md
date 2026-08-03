<!--
Hand-written, unlike docs/rules/. It records why the model graph is shaped the
way it is, which is the part no generator can read off the code.
-->

# The model graph

Almost every `DJA` and `DJD` rule asks a question one file cannot answer.
*Does this serializer expose a password?* depends on what `Meta.model` names
and what that model inherits. *Is this list endpoint scoped to the caller?*
depends on whether the model it returns can reach the user model at all.
*Is this cascade dangerous?* depends on which side of the `ForeignKey` the
deletion starts from.

So before any rule runs, `djaudit.graph` reads every model in the project once
and builds something the rules can interrogate. This note is about the
decisions in that structure — the ones a reader of the code would otherwise
have to reconstruct.

## What it is built from

Source, and only source. The static tier never imports the target, so the graph
is assembled from `ast` alone:

| Module | Reads |
|---|---|
| `builder` | Which class statements are models, one node each |
| `fields` | Field declarations in a class body |
| `meta` | `class Meta` — `ordering`, `indexes`, `constraints`, `abstract`, `proxy` |
| `managers` | What `Model.objects` actually is |
| `relations` | `ForeignKey`/`OneToOne`/`ManyToMany`, and what they point at |
| `inheritance` | Base classes, including across modules |
| `inherit` | What a subclass gets from an ancestor |
| `queries` | The questions rules ask of the finished graph |

Not importing is a constraint with teeth. Django resolves a great deal at
import time — `AppConfig.label`, swappable models, abstract inheritance, and
whatever a third-party metaclass decides to add — and none of it runs here.
Each of the sections below is a consequence.

## Three names for one model

Django refers to a model three ways: by class, by `"app.Model"`, and by bare
`"Model"`. All three turn up in real code, often in one class body:

```python
class Order(models.Model):
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    coupon = models.ForeignKey(Coupon, on_delete=models.PROTECT)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True)
    plan = models.ForeignKey("billing.Plan", on_delete=models.PROTECT)
```

`ModelGraph` indexes all of them. A graph that supported one form would make
every caller reimplement the other two, and each caller would get the ambiguity
wrong in its own way — a bare `"Coupon"` is unambiguous only while one app
declares it, and the graph knows when that stops being true.

`settings.AUTH_USER_MODEL` is a fourth case and the interesting one: it is a
reference whose value lives in a different file. The edge records that it was
written that way rather than resolving it to a string, so the graph can point
it at whatever the project configured.

## The app label is not the directory name

`ForeignKey("pretixbase.Order")` resolves against the *app label*, which
defaults to the directory name and is overridden by `AppConfig.label` in
`apps.py`. pretix does exactly that: `src/pretix/base` declares
`label = "pretixbase"`, and every relation in the project uses it.

Reading `apps.py` is therefore not a refinement. Without it, a large project's
relations resolve to nothing while the graph still looks complete — it answers
every question confidently and wrongly, which is worse than crashing.

## Inheritance is where most of the fields are

A model's own class body is often the least interesting part of it. NetBox
declares 935 fields directly and inherits 1475. The graph resolves inheritance
across module boundaries and applies what an ancestor declares — fields,
`Meta`, managers — to the class that inherits it, keeping the three kinds
Django treats differently apart:

| Kind | What it is | How the graph treats it |
|---|---|---|
| Abstract base | `Meta.abstract = True` | No table. Its fields are copied into each concrete child. |
| Multi-table | A concrete parent | A table of its own, plus an implicit link from the child. |
| Proxy | `Meta.proxy = True` | No table and no fields of its own; another Python view of the parent's. |

`ModelNode.is_concrete` is the distinction most rules want. An abstract base
has no rows, so a rule about what a query returns has nothing to say about one.

## Bases outside the project are recorded, not guessed

When a model inherits `MPTTModel`, `AbstractBaseUser`, or any base from an
installed package, the static tier cannot read it. The graph records the
unresolved base by name in `ModelGraph.unresolved_bases` instead of dropping it
silently or inventing fields for it.

That record is what lets a rule stay quiet honestly. A rule needing the
complete field list of a model with an unreadable ancestor can tell that it is
missing something and decline, rather than reporting on a partial picture. It
is also what makes the coverage gate meaningful: a field the graph does not
have is either attributable to a named external base or it is a defect, and
`scripts/graph_coverage.py` fails the build on the second kind.

## The user model is a first-class question

Authorization rules keep asking one thing in different words: *does this row
belong to somebody?* The graph answers it directly.

`user_model` resolves `AUTH_USER_MODEL`, defaulting to `auth.User` when the
setting is absent — getting this wrong is not a small error, because a project
with a custom user model would have every ownership question answered against
a model it does not use. `path_to_user()` then walks relations to find whether
a model reaches it, returning the path rather than a boolean so a finding can
say *how*. `is_user_owned()` is the common case built on top.

The path matters because ownership is rarely direct. `OrderLine` has no user
column; it points at `Order`, which points at `Customer`, which points at the
user. A rule looking one hop would call `OrderLine` unowned and say nothing
about an endpoint returning every customer's order lines.

## Reverse edges, because half the questions run backwards

`ModelGraph.incoming` indexes relations by target. *What cascades when a user
is deleted?* and *what would this nested serializer field reach?* are both
backwards questions, and recomputing them by scanning every model's outgoing
edges is how an analyzer becomes quadratic on a repository of 1,200 files.

Reverse accessors are resolved at the same time, because their names are not
mechanical: `related_name` may be interpolated (`"%(class)s_set"`), may end in
`+` to suppress the accessor entirely, and a symmetrical `ManyToManyField` to
`self` has no reverse side at all.

## What the graph deliberately does not do

- **It does not read migrations.** The graph describes the models as declared
  today. Migrations describe how they got there — a different question, and
  `DJM`'s.
- **It does not evaluate expressions.** `ForeignKey(get_model(), ...)` is
  recorded as a relation with no known target. Rules see the absence rather
  than a guess.
- **It does not resolve models created at runtime.** django-hierarkey generates
  `Event_SettingsStore` with no `class` statement anywhere in pretix's source.
  There is nothing there for a reader of source to find, and the graph says so
  instead of pretending otherwise.
- **It does not import, ever.** Including `settings.py`. Everything above
  follows from that one line.

## Checking it against something that is not itself

Unit tests confirm the graph does what its author expected. They cannot confirm
the author understood Django — and a graph that silently dropped half a project
would still report nothing, still crash on nothing, and still score 100%
precision on every benchmark.

`scripts/graph_coverage.py` compares the graph against the target's own
migrations, which Django wrote by introspecting live model classes with every
package installed and every metaclass run. It replays `CreateModel`, `AddField`,
`RenameField` and the rest with `ast`, asks how much of that the graph found,
and requires every gap to be attributed to a named cause. The floors enforced
in CI:

| Target | Models | Fields |
|---|---|---|
| Healthchecks | 12/12 | 127/127 |
| NetBox | 144/145 | 1795/1856 |
| pretix | 103/113 | 1071/1075 |

NetBox's 61 missing fields are 55 from `MPTTModel`, 4 from `AbstractBaseUser`
and 2 from `TagBase` — every one an ancestor in site-packages. That is what the
constraint at the top of this page costs, stated rather than rounded off.
