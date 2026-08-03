"""Rules about what a serializer publishes, and what it will accept.

Step 2.3 asked who may reach an endpoint. These ask what comes back when they
do, and what they may send. The questions are independent: a correctly
permissioned endpoint still leaks if its serializer ships a password hash, and
a serializer with a careful field list still grants privileges if one of those
fields is the owner.

Every rule here judges a serializer alone, not a request path. That is
deliberate. A serializer is reused across views far more often than it is
written, so a field list that is wrong is wrong everywhere, and tying the
finding to one endpoint would report the same defect once per route while
implying the other routes were fine. The cost is that we cannot say whether a
given serializer is reachable, which each rule states plainly rather than
dressing up as certainty.

The recurring trap is that DRF's defaults are not neutral. `fields` is not
merely a list: `'__all__'` and `exclude` both hand the decision to the model,
so a column added by a migration months later is published by a file nobody
edited.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from djaudit.context import ProjectContext
from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)
from djaudit.registry import Rule, RuleMeta, register
from djaudit.rules.authorization import OWNERSHIP_FIELDS

if TYPE_CHECKING:
    from djaudit.api.serializers import SerializerNode
    from djaudit.graph.nodes import ModelNode


class SerializerRule(Rule):
    """Base for rules that judge one serializer at a time.

    Carries the loop and the two exclusions every rule here shares, so a
    subclass is only its own judgement.
    """

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for node in ctx.api_surface.serializers.values():
            if not node.is_model_serializer:
                # A plain Serializer publishes exactly what it declares, so
                # there is no model behind it to over-share.
                continue
            if node.meta_inherited:
                # No `Meta` of its own: an abstract mixin whose field list is
                # decided by whichever concrete subclass uses it. Judging it
                # here would report the mixin and miss the class that ships.
                continue
            yield from self.inspect(ctx, node)

    def inspect(self, ctx: ProjectContext, node: SerializerNode) -> Iterator[Finding]:
        raise NotImplementedError

    def at(self, ctx: ProjectContext, node: SerializerNode, line: int | None = None) -> Location:
        where = line or node.lineno
        return Location(
            file=ctx.rel(node.path),
            line=where,
            snippet=ctx.snippet(node.path, where),
        )

    def model_evidence(self, ctx: ProjectContext, node: SerializerNode) -> Evidence | None:
        """The columns the model has today, since that is the real field list.

        A reader looking at ``fields = '__all__'`` sees one line. The useful
        reply is what that line expands to, because the argument for changing
        it is usually a column in the list they had forgotten was there.
        """
        if node.model is None:
            return None
        model = ctx.model_graph.get(node.model)
        if model is None:
            return None
        names = sorted(model.fields)
        if not names:
            return None
        return Evidence(
            kind=EvidenceKind.AST,
            content=f"{model.label} currently has: {', '.join(names)}",
            source="djaudit model graph",
        )


@register
class OpenFieldList(SerializerRule):
    meta = RuleMeta(
        id="DJA-008",
        title="Serializer publishes every model field",
        family=Family.DJA,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "fields = '__all__' does not name a field list, it delegates the decision "
            "to the model. Whatever columns the model has at import time are what the "
            "API returns and accepts, so a later migration publishes a new column "
            "without anyone editing this file or reviewing this serializer. The field "
            "that leaks this way is rarely one anybody would have listed on purpose."
        ),
        remediation=(
            "Replace '__all__' with an explicit list of the fields this endpoint is "
            "meant to expose. It is longer to write once and then fails closed "
            "forever: a new column stays invisible until somebody adds it here "
            "deliberately, which is the review the migration did not get."
        ),
        references=(
            "https://www.django-rest-framework.org/api-guide/serializers/#specifying-which-fields-to-include",
            "https://cwe.mitre.org/data/definitions/213.html",
        ),
        limitations=(
            "Whether any routed view uses this serializer is not checked, so one "
            "reached only by a management command reads the same as a public one.",
            "Severity does not rise when the model carries an obviously sensitive "
            "column, because that judgement is DJA-010's and two findings on one "
            "line with one fix between them is one finding too many.",
        ),
    )

    def inspect(self, ctx: ProjectContext, node: SerializerNode) -> Iterator[Finding]:
        if node.mode != "all":
            return
        model = node.model or node.model_ref or "its model"
        evidence = [
            Evidence(
                kind=EvidenceKind.AST,
                content=f"{node.name}.Meta.fields = '__all__'",
                source=ctx.rel(node.path),
            )
        ]
        columns = self.model_evidence(ctx, node)
        if columns is not None:
            evidence.append(columns)
        yield self.finding(
            location=self.at(ctx, node),
            message=(
                f"{node.name} sets fields = '__all__', so every column on "
                f"{model} is published and every column added later will be too."
            ),
            evidence=tuple(evidence),
        )


@register
class ExcludeFieldList(SerializerRule):
    meta = RuleMeta(
        id="DJA-009",
        title="Serializer names what to hide instead of what to show",
        family=Family.DJA,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "Meta.exclude publishes every field the model has except the named ones, "
            "so the list is a denylist and the default is exposure. It reads as more "
            "careful than '__all__' because somebody clearly thought about which "
            "fields to hide, but the reasoning only covers the columns that existed "
            "when it was written: a column added later is published by default, and "
            "the file that decides it was last edited before that column existed."
        ),
        remediation=(
            "Switch to Meta.fields with the columns this endpoint should return. The "
            "two spellings look interchangeable and are not -- one fails open on "
            "every future migration and the other fails closed, which matters most "
            "for the column nobody has thought of yet."
        ),
        references=(
            "https://www.django-rest-framework.org/api-guide/serializers/#specifying-which-fields-to-include",
            "https://cwe.mitre.org/data/definitions/213.html",
        ),
        limitations=(
            "Whether any routed view uses this serializer is not checked, so one "
            "reached only by a management command reads the same as a public one.",
            "The excluded names are reported as written and not checked against the "
            "model, so a typo in an exclude entry silently publishes the field it "
            "was meant to hide and is not distinguished from a correct entry.",
        ),
    )

    def inspect(self, ctx: ProjectContext, node: SerializerNode) -> Iterator[Finding]:
        if node.mode != "exclude":
            return
        model = node.model or node.model_ref or "its model"
        hidden = ", ".join(node.exclude) if node.exclude else "(unreadable)"
        evidence = [
            Evidence(
                kind=EvidenceKind.AST,
                content=f"{node.name}.Meta.exclude = [{hidden}]",
                source=ctx.rel(node.path),
            )
        ]
        columns = self.model_evidence(ctx, node)
        if columns is not None:
            evidence.append(columns)
        yield self.finding(
            location=self.at(ctx, node),
            message=(
                f"{node.name} hides {len(node.exclude)} field(s) and publishes the "
                f"rest of {model}, including any column added to it later."
            ),
            evidence=tuple(evidence),
        )


SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "password_hash",
        "secret",
        "secret_key",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "api_key",
        "apikey",
        "private_key",
        "signing_key",
        "key",
        "salt",
        "otp_secret",
        "totp_secret",
        "session_key",
    }
)
"""Field names that name credential material rather than describe it.

Deliberately excludes `hash`, which on NetBox's DataFile is a checksum of
public content, and every `*_id` spelling, which names a reference rather than
a secret. `key` is kept despite being the most generic entry here, because the
one thing it reliably names in a Django project is an API token; the ambiguity
is recorded in the rule's limitations rather than resolved by dropping it.
"""

PRIVILEGE_FIELDS: frozenset[str] = frozenset(
    {
        "is_staff",
        "is_superuser",
        "is_admin",
        "is_active",
        "permissions",
        "user_permissions",
        "groups",
    }
)
"""Field names that grant authority -- *on the user model only*.

Every one of these is an ordinary domain word somewhere else. NetBox has
`is_active` on a cable path and on a config context, and `groups` on a contact
and on a notification group; none of them decides anything about a session. A
name is not evidence, so this set is only consulted once the serializer's model
is known to be the project's user model, which is the same discipline that took
the mass-assignment candidates from 270 to 8.
"""


@register
class SensitiveField(SerializerRule):
    meta = RuleMeta(
        id="DJA-010",
        title="Serializer returns a sensitive field",
        family=Family.DJA,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "A field named in Meta.fields is returned in every response the "
            "serializer renders, so credential material listed there is handed to "
            "everyone the endpoint answers. DRF has a spelling for the other "
            "intention -- write_only=True accepts a value on input and never renders "
            "it -- and the two differ by one keyword in a field list that otherwise "
            "looks identical. Marking such a field read_only does not help: it "
            "prevents writes and guarantees reads, which is the wrong half."
        ),
        remediation=(
            "If the value is only ever supplied by the client, set write_only=True on "
            "the field or via extra_kwargs. If it is generated server-side and must "
            "be shown once, return it from the create response only and drop it from "
            "the serializer used for list and retrieve, so it is not re-served on "
            "every subsequent read of the same record."
        ),
        references=(
            "https://www.django-rest-framework.org/api-guide/fields/#write_only",
            "https://cwe.mitre.org/data/definitions/522.html",
            "https://owasp.org/API-Security/editions/2023/en/0xa3-broken-object-property-level-authorization/",
        ),
        limitations=(
            "The judgement is made on the field name, so a column called key or "
            "secret that holds something harmless is reported and a credential "
            "stored under a domain-specific name is missed entirely.",
            "Endpoints that deliberately issue a credential once, such as a token "
            "provisioning view, are indistinguishable from ones that re-serve a "
            "stored secret on every read, and both are reported.",
            "Whether any routed view uses this serializer is not checked, so one "
            "reached only by a management command reads the same as a public one.",
        ),
    )

    @staticmethod
    def serializes_the_user(ctx: ProjectContext, node: SerializerNode) -> bool:
        """Whether this serializer's model is the one that holds authority.

        The graph only contains models defined in the project, so a project on
        Django's built-in ``auth.User`` -- which is most of them -- resolves
        ``Meta.model = User`` to nothing at all. Falling back to the class name
        keeps the common case working; a project model genuinely called ``User``
        that exposes ``is_superuser`` is worth a look regardless.
        """
        user_model = ctx.model_graph.user_model
        if node.model is not None:
            return node.model == user_model
        return node.model_ref is not None and (
            node.model_ref.rsplit(".", 1)[-1] == user_model.rsplit(".", 1)[-1]
        )

    def inspect(self, ctx: ProjectContext, node: SerializerNode) -> Iterator[Finding]:
        is_user = self.serializes_the_user(ctx, node)
        seen: set[str] = set()
        for name in node.fields:
            if name in seen:
                continue
            if node.is_write_only(name):
                # Accepted on input, never rendered. The correct pattern, and
                # the one NetBox uses for `password` -- reporting it would
                # teach readers to ignore this rule.
                continue
            if name in SECRET_FIELDS:
                kind = "credential material"
                severity = Severity.HIGH
            elif is_user and name in PRIVILEGE_FIELDS:
                # Disclosure, not theft: knowing who is staff tells an attacker
                # which account to spend effort on, but does not hand them one.
                kind = "an authority-granting field on the user model"
                severity = Severity.MEDIUM
            else:
                continue
            seen.add(name)
            note = " (read_only, so returned but not accepted)" if node.is_read_only(name) else ""
            yield self.finding(
                location=self.at(ctx, node, node.field_line(name)),
                severity=severity,
                message=(
                    f"{node.name} returns {name!r}, {kind}, to every caller the endpoint answers."
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=(
                            f"{node.name}.Meta.fields includes {name!r}{note}"
                            f"; write_only is not set"
                        ),
                        source=ctx.rel(node.path),
                    ),
                ),
            )


@register
class WritableOwnership(SerializerRule):
    meta = RuleMeta(
        id="DJA-011",
        title="Serializer accepts the field that decides who owns the row",
        family=Family.DJA,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "A field in Meta.fields is writable unless something marks it otherwise, "
            "so a foreign key to the user model listed there lets the caller choose "
            "whose record this becomes. That is not a data-entry mistake, it is an "
            "authorization one: the endpoint's permission check asks whether this "
            "caller may create a record, and then the request body answers the "
            "different question of who the record belongs to. Ownership is normally "
            "the thing every other permission check is built on, so getting it from "
            "the request inverts the rest of the model."
        ),
        remediation=(
            "Add the field to Meta.read_only_fields and set it in the view, with "
            "serializer.save(user=self.request.user) in perform_create. If the value "
            "genuinely must come from the request -- an administrator creating a "
            "record on someone's behalf -- keep it writable and check the caller's "
            "authority in validate(), the way NetBox's TokenSerializer does."
        ),
        references=(
            "https://www.django-rest-framework.org/api-guide/generic-views/#methods",
            "https://cwe.mitre.org/data/definitions/915.html",
            "https://owasp.org/API-Security/editions/2023/en/0xa3-broken-object-property-level-authorization/",
        ),
        limitations=(
            "A hand-written authority check in validate() or create() is not "
            "detected, so a serializer that deliberately allows the assignment and "
            "guards it in Python is reported the same as one that does not.",
            "Only foreign keys resolving to the project's user model count, so "
            "ownership expressed through an intermediate profile or membership "
            "model is not recognised and is missed entirely.",
            "Whether any routed view uses this serializer for writes is not "
            "checked, so a read-only endpoint's serializer reads the same as one "
            "behind a create or update route.",
        ),
    )

    def inspect(self, ctx: ProjectContext, node: SerializerNode) -> Iterator[Finding]:
        if node.model is None:
            return
        model = ctx.model_graph.get(node.model)
        if model is None:
            return
        owning = {
            edge.field_name: edge
            for edge in model.relations
            if edge.points_at_user and edge.field_name in OWNERSHIP_FIELDS
        }
        for name in dict.fromkeys(node.fields):
            edge = owning.get(name)
            if edge is None:
                continue
            if node.is_read_only(name) or self.unwritable(node, model, name):
                continue
            yield self.finding(
                location=self.at(ctx, node, node.field_line(name)),
                message=(
                    f"{node.name} accepts {name!r} from the request, so a caller "
                    f"can create or move a {model.name} owned by someone else."
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=(
                            f"{node.name}.Meta.fields includes {name!r}, and neither "
                            f"read_only_fields nor extra_kwargs marks it read-only"
                        ),
                        source=ctx.rel(node.path),
                    ),
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=(
                            f"{model.label}.{name} is a {edge.kind} to {ctx.model_graph.user_model}"
                        ),
                        source="djaudit model graph",
                    ),
                ),
            )

    @staticmethod
    def unwritable(node: SerializerNode, model: ModelNode, name: str) -> bool:
        """Whether DRF will refuse the write regardless of the field list.

        Three ways, and missing any of them is what made the first draft of this
        rule report 270 fields on NetBox. `build_standard_field_kwargs` sets
        `read_only` for an AutoField or any field with `editable=False` before
        it looks at anything else, so a primary key can never be assigned and
        neither can an `auto_now` timestamp. And `HiddenField` sets
        `write_only` on itself and takes no input at all -- it is the correct
        way to default a field to the request's user, so reporting it would
        report the fix.
        """
        declared = node.declared.get(name)
        kind = declared.kind if declared is not None else None
        if kind is not None and kind.rsplit(".", 1)[-1] == "HiddenField":
            return True
        field = model.fields.get(name)
        if field is None:
            return False
        return field.primary_key or not field.editable or field.auto_now or field.auto_now_add
