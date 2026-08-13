"""The generate-audit-repair loop, and the three ways it could cheat.

Any loop that iterates until an auditor is silent has degenerate optima, and
they are the whole subject of this file. Deleting the code clears every
finding. Suppressing the rule clears the finding. Writing to `settings.py`
clears findings in a file the caller never asked to be touched. Each is closed
structurally rather than by asking the model nicely, and each gets a test that
tries it.

The provider here is scripted rather than mocked at the boundary: it returns
the payloads a real model would return, including the bad ones. That keeps the
tests about the loop's decisions rather than about how a call was made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.generate import (
    Loop,
    Outcome,
    Spec,
    commit,
    first_prompt,
    repair_prompt,
    surface_of,
    write_app,
)
from djaudit.generate.loop import MAX_SYNTAX_REPAIRS, in_app, summarise
from djaudit.generate.scaffold import SCHEMA, WRITABLE
from djaudit.llm.provider import Answer, Declined, Prompt, Reply, SchemaViolationError

SETTINGS = """\
import os

SECRET_KEY = os.environ['DJANGO_SECRET_KEY']
DEBUG = False
ALLOWED_HOSTS = ['example.com']
INSTALLED_APPS = ['shop']
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
X_FRAME_OPTIONS = 'DENY'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
"""

# What a model writes when it is not thinking about an auditor. Measured, not
# invented: this is the shape that produced six findings when the tool was
# first demonstrated.
CARELESS = {
    "models_py": (
        "from django.db import models\n\n\n"
        "class Order(models.Model):\n"
        "    reference = models.CharField(max_length=32, null=True)\n"
        "    customer = models.ForeignKey('auth.User', on_delete=models.CASCADE)\n"
    ),
    "serializers_py": (
        "from rest_framework import serializers\n\n"
        "from shop.models import Order\n\n\n"
        "class OrderSerializer(serializers.ModelSerializer):\n"
        "    class Meta:\n"
        "        model = Order\n"
        '        fields = "__all__"\n'
    ),
    "views_py": (
        "from rest_framework import viewsets\n\n"
        "from shop.models import Order\n"
        "from shop.serializers import OrderSerializer\n\n\n"
        "class OrderViewSet(viewsets.ModelViewSet):\n"
        "    queryset = Order.objects.all()\n"
        "    serializer_class = OrderSerializer\n"
    ),
    "urls_py": (
        "from rest_framework.routers import DefaultRouter\n\n"
        "from shop.views import OrderViewSet\n\n"
        "router = DefaultRouter()\n"
        'router.register("orders", OrderViewSet)\n'
        "urlpatterns = router.urls\n"
    ),
    "admin_py": (
        "from django.contrib import admin\n\n"
        "from shop.models import Order\n\n"
        "admin.site.register(Order)\n"
    ),
}

# A plausible repair, and a measured surprise: it clears DJA-002, DJA-008 and
# DJD-002 and *introduces* two findings that were not there before. Naming the
# serializer fields explicitly makes `customer` writable (DJA-011), and adding
# pagination to an unordered queryset makes page two nondeterministic (DJD-003).
# 5 findings become 4. This is why the loop audits after every iteration rather
# than after the last one.
PARTIAL = {
    **CARELESS,
    "models_py": (
        "from django.db import models\n\n\n"
        "class Order(models.Model):\n"
        '    reference = models.CharField(max_length=32, blank=True, default="")\n'
        "    customer = models.ForeignKey(\n"
        '        "auth.User", on_delete=models.CASCADE, related_name="orders"\n'
        "    )\n"
    ),
    "serializers_py": (
        "from rest_framework import serializers\n\n"
        "from shop.models import Order\n\n\n"
        "class OrderSerializer(serializers.ModelSerializer):\n"
        "    class Meta:\n"
        "        model = Order\n"
        '        fields = ("id", "reference", "customer")\n'
    ),
    "views_py": (
        "from rest_framework import viewsets\n"
        "from rest_framework.pagination import PageNumberPagination\n"
        "from rest_framework.permissions import IsAuthenticated\n\n"
        "from shop.models import Order\n"
        "from shop.serializers import OrderSerializer\n\n\n"
        "class OrderViewSet(viewsets.ModelViewSet):\n"
        '    queryset = Order.objects.select_related("customer").all()\n'
        "    serializer_class = OrderSerializer\n"
        "    permission_classes = [IsAuthenticated]\n"
        "    pagination_class = PageNumberPagination\n"
    ),
}

# The repair that actually lands: 0 findings in the app, every model, field,
# serializer and viewset still there. Measured, not asserted.
CLEAN = {
    **CARELESS,
    "models_py": (
        "from django.db import models\n\n\n"
        "class Order(models.Model):\n"
        '    reference = models.CharField(max_length=32, blank=True, default="")\n'
        "    customer = models.ForeignKey(\n"
        '        "auth.User", on_delete=models.PROTECT, related_name="orders"\n'
        "    )\n\n"
        "    class Meta:\n"
        '        ordering = ("id",)\n'
    ),
    "serializers_py": (
        "from rest_framework import serializers\n\n"
        "from shop.models import Order\n\n\n"
        "class OrderSerializer(serializers.ModelSerializer):\n"
        "    class Meta:\n"
        "        model = Order\n"
        '        fields = ("id", "reference", "customer")\n'
        '        read_only_fields = ("customer",)\n'
    ),
    "views_py": (
        "from rest_framework import viewsets\n"
        "from rest_framework.pagination import PageNumberPagination\n"
        "from rest_framework.permissions import IsAuthenticated\n\n"
        "from shop.models import Order\n"
        "from shop.serializers import OrderSerializer\n\n\n"
        "class OrderViewSet(viewsets.ModelViewSet):\n"
        "    serializer_class = OrderSerializer\n"
        "    permission_classes = [IsAuthenticated]\n"
        "    pagination_class = PageNumberPagination\n\n"
        "    def get_queryset(self):\n"
        "        return Order.objects.filter(customer=self.request.user).select_related(\n"
        '            "customer"\n'
        "        )\n\n"
        "    def perform_create(self, serializer):\n"
        "        serializer.save(customer=self.request.user)\n"
    ),
}

# The degenerate optimum: every finding is gone because the feature is.
GUTTED = {
    "models_py": "from django.db import models\n",
    "serializers_py": "from rest_framework import serializers\n",
    "views_py": "from rest_framework import viewsets\n",
    "urls_py": "urlpatterns = []\n",
    "admin_py": "from django.contrib import admin\n",
}


class Scripted:
    """Returns prepared replies in order, and remembers what it was asked."""

    def __init__(self, *replies: dict[str, str] | Declined) -> None:
        self.replies = list(replies)
        self.prompts: list[Prompt] = []

    @property
    def name(self) -> str:
        return "scripted"

    def ask(self, prompt: Prompt) -> Reply:
        self.prompts.append(prompt)
        if not self.replies:
            return Declined("the script ran out")
        nxt = self.replies.pop(0)
        if isinstance(nxt, Declined):
            return nxt
        return Answer(content=dict(nxt), model="scripted")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real Django project for the app to be generated into and audited in."""
    root = tmp_path / "site"
    (root / "conf").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'conf.settings')\n"
    )
    (root / "conf" / "__init__.py").write_text("")
    (root / "conf" / "settings.py").write_text(SETTINGS)
    return root


def spec_for(project: Path) -> Spec:
    return Spec(app="shop", description="orders placed by customers", project=project)


def loop_over(*replies: dict[str, str] | Declined, **kwargs: object) -> tuple[Loop, Scripted]:
    provider = Scripted(*replies)
    return Loop(provider=provider, **kwargs), provider  # type: ignore[arg-type]


def drive(spec: Spec, *replies: dict[str, str] | Declined, **kwargs: object):
    loop, provider = loop_over(*replies, **kwargs)
    result = loop.run(spec, first_prompt(spec), lambda f, s: repair_prompt(spec, f, s))
    return result, provider


class TestTheModelCannotWriteWhereItWasNotAsked:
    """The response schema is the whole of this guarantee."""

    def test_the_writable_set_is_closed(self):
        declared = {f.name for f in SCHEMA.fields}
        assert declared == {field for field, _, _ in WRITABLE}

    def test_a_reply_naming_settings_is_refused_before_it_reaches_a_filesystem(self):
        """The dangerous version of this feature writes whatever comes back."""
        with pytest.raises(SchemaViolationError, match="never declared"):
            SCHEMA.validate({**CARELESS, "settings_py": "DEBUG = True"})

    def test_a_reply_naming_a_traversal_path_is_refused(self):
        with pytest.raises(SchemaViolationError):
            SCHEMA.validate({**CARELESS, "../../etc/cron.d/x": "* * * * * root sh"})

    def test_the_control_the_declared_set_is_accepted(self):
        """Every refusal above would pass on a schema that refused everything."""
        assert SCHEMA.validate(CARELESS) == CARELESS

    def test_no_writable_file_escapes_the_app_directory(self):
        for _, filename, _ in WRITABLE:
            assert ".." not in filename
            assert not filename.startswith("/")


class TestWritingTheApp:
    def test_it_writes_the_files_the_model_returned(self, project: Path, tmp_path: Path):
        write_app(spec_for(project), CARELESS, tmp_path)
        for _, filename, _ in WRITABLE:
            assert (tmp_path / "shop" / filename).is_file()

    def test_it_writes_the_boilerplate_the_model_did_not(self, project: Path, tmp_path: Path):
        """Nothing is gained by spending a model call on default_auto_field."""
        write_app(spec_for(project), CARELESS, tmp_path)
        assert (tmp_path / "shop" / "__init__.py").is_file()
        assert (tmp_path / "shop" / "migrations" / "__init__.py").is_file()
        assert "BigAutoField" in (tmp_path / "shop" / "apps.py").read_text()

    def test_a_fenced_block_is_unwrapped(self, project: Path, tmp_path: Path):
        """Structured output does not always stop a model adding a fence."""
        fenced = {**CARELESS, "admin_py": "```python\nfrom django.contrib import admin\n```"}
        write_app(spec_for(project), fenced, tmp_path)
        written = (tmp_path / "shop" / "admin.py").read_text()
        assert written.startswith("from django.contrib")
        assert "```" not in written

    def test_a_missing_file_is_skipped_rather_than_written_empty(
        self, project: Path, tmp_path: Path
    ):
        partial = {k: v for k, v in CARELESS.items() if k != "admin_py"}
        write_app(spec_for(project), partial, tmp_path)
        assert not (tmp_path / "shop" / "admin.py").exists()

    def test_an_app_name_that_is_not_an_identifier_is_refused(self, project: Path):
        with pytest.raises(ValueError, match="not a usable app name"):
            Spec(app="my-app", description="x", project=project)

    def test_an_empty_description_is_refused(self, project: Path):
        with pytest.raises(ValueError, match="no description"):
            Spec(app="shop", description="   ", project=project)


class TestTheSurfaceDetector:
    """Repair-by-deletion is the degenerate optimum; this is what catches it."""

    def test_it_sees_the_models_and_their_fields(self):
        surface = surface_of(CARELESS)
        assert "Order" in surface.classes
        assert ("Order", "reference") in surface.fields

    def test_it_sees_serializers_and_viewsets(self):
        surface = surface_of(CARELESS)
        assert {"OrderSerializer", "OrderViewSet"} <= surface.classes

    def test_gutting_the_app_is_a_regression(self):
        assert surface_of(CARELESS).missing_from(surface_of(GUTTED))

    def test_the_regression_names_the_casualty(self):
        """A count that went down is not something anyone can act on."""
        lost = surface_of(CARELESS).missing_from(surface_of(GUTTED))
        assert "Order" in lost.describe()

    def test_removing_one_field_is_a_regression(self):
        thinner = {
            **CARELESS,
            "models_py": (
                "from django.db import models\n\n\n"
                "class Order(models.Model):\n"
                "    customer = models.ForeignKey('auth.User', on_delete=models.CASCADE)\n"
            ),
        }
        lost = surface_of(CARELESS).missing_from(surface_of(thinner))
        assert lost.fields == (("Order", "reference"),)

    def test_a_rename_reads_as_a_removal_and_that_is_deliberate(self):
        """Renaming the caller's models mid-repair is something they should see."""
        renamed = {**CARELESS, "models_py": CARELESS["models_py"].replace("Order", "PurchaseOrder")}
        assert surface_of(CARELESS).missing_from(surface_of(renamed))

    def test_a_repair_that_keeps_everything_is_not_a_regression(self):
        """The control. Without it, every assertion above passes on a detector
        that reports a regression for any change at all."""
        assert not surface_of(CARELESS).missing_from(surface_of(CLEAN))

    def test_a_method_added_during_repair_is_not_a_regression(self):
        """CLEAN adds get_queryset and perform_create; neither is a loss."""
        assert not surface_of(CARELESS).missing_from(surface_of(PARTIAL))

    def test_adding_a_model_is_not_a_regression(self):
        wider = {
            **CARELESS,
            "models_py": CARELESS["models_py"]
            + "\n\nclass Note(models.Model):\n    body = models.TextField()\n",
        }
        assert not surface_of(CARELESS).missing_from(surface_of(wider))

    def test_unparseable_source_contributes_nothing_rather_than_raising(self):
        """A syntax error is the loop's business; this must not turn it into a traceback."""
        assert surface_of({"models_py": "class Order(models.Model:"}).empty


class TestTheLoop:
    def test_a_clean_first_attempt_stops_immediately(self, project: Path):
        result, provider = drive(spec_for(project), CLEAN)
        assert result.outcome is Outcome.CLEAN
        assert len(result.iterations) == 1
        assert len(provider.prompts) == 1

    def test_a_careless_attempt_is_repaired(self, project: Path):
        """The measurement the whole phase exists for, in miniature."""
        result, _ = drive(spec_for(project), CARELESS, CLEAN)
        assert result.outcome is Outcome.CLEAN
        assert len(result.iterations[0].findings) == 5
        assert result.iterations[-1].findings == ()
        assert result.cleared == 5

    def test_the_findings_are_shown_to_the_model(self, project: Path):
        """A repair prompt that did not carry them would be a second guess."""
        _, provider = drive(spec_for(project), CARELESS, CLEAN)
        assert len(provider.prompts) == 2
        assert "DJA-008" in provider.prompts[1].user

    def test_the_current_code_is_shown_to_the_model(self, project: Path):
        _, provider = drive(spec_for(project), CARELESS, CLEAN)
        assert "class OrderViewSet" in provider.prompts[1].user

    def test_a_repair_that_introduces_a_finding_does_not_stop_the_loop(self, project: Path):
        """Measured, not supposed. PARTIAL clears three findings and adds two,
        so an implementation that stopped at 'fewer than last time' would ship
        an app with a fresh DJA-011 in it."""
        result, _ = drive(spec_for(project), CARELESS, PARTIAL, CLEAN)
        assert [len(i.findings) for i in result.iterations] == [5, 4, 0]
        assert result.outcome is Outcome.CLEAN
        introduced = result.iterations[1].fingerprints - result.iterations[0].fingerprints
        assert introduced

    def test_no_model_means_nothing_is_generated(self, project: Path):
        result, _ = drive(spec_for(project), Declined("no key"))
        assert result.outcome is Outcome.DECLINED
        assert result.reason == "no key"
        assert result.iterations == []


class TestTheCeiling:
    def test_gutting_the_app_is_refused(self, project: Path):
        """An empty file passes every rule djaudit has. It is not a repair."""
        result, _ = drive(spec_for(project), CARELESS, GUTTED)
        assert result.outcome is Outcome.REGRESSED
        assert result.regression is not None
        assert "Order" in result.reason

    def test_a_regressed_run_is_never_written(self, project: Path):
        result, _ = drive(spec_for(project), CARELESS, GUTTED)
        assert not result.accepted
        with pytest.raises(ValueError, match="not written"):
            commit(result)

    def test_a_suppression_is_refused(self, project: Path):
        """The second degenerate optimum."""
        cheating = {**CARELESS, "views_py": CARELESS["views_py"] + "# djaudit: ignore\n"}
        result, _ = drive(spec_for(project), cheating)
        assert result.outcome is Outcome.SUPPRESSED
        assert "not a fix" in result.reason

    def test_a_suppressed_run_is_never_written(self, project: Path):
        cheating = {**CARELESS, "views_py": CARELESS["views_py"] + "# djaudit: ignore\n"}
        result, _ = drive(spec_for(project), cheating)
        with pytest.raises(ValueError, match="not written"):
            commit(result)

    def test_output_that_does_not_parse_is_refused(self, project: Path):
        broken = {**CARELESS, "models_py": "class Order(models.Model:\n"}
        result, _ = drive(spec_for(project), broken)
        assert result.outcome is Outcome.UNPARSEABLE
        assert "models.py" in result.reason

    def test_a_stalled_run_stops_before_the_ceiling(self, project: Path):
        """An iteration that clears nothing will not clear anything next time."""
        result, provider = drive(spec_for(project), CARELESS, CARELESS, CARELESS, CARELESS)
        assert result.outcome is Outcome.STALLED
        assert len(result.iterations) == 2
        assert len(provider.prompts) == 2

    def test_the_ceiling_stops_a_run_that_is_still_improving(self, project: Path):
        """Slow progress is still bounded."""
        result, _ = drive(spec_for(project), CARELESS, PARTIAL, max_iterations=2)
        assert result.outcome is Outcome.EXHAUSTED
        assert "outstanding" in result.reason

    def test_a_model_that_stops_answering_exhausts_rather_than_crashes(self, project: Path):
        result, _ = drive(spec_for(project), CARELESS, Declined("budget spent"))
        assert result.outcome is Outcome.EXHAUSTED
        assert "budget spent" in result.reason

    @pytest.mark.parametrize(
        ("outcome", "writable"),
        [
            (Outcome.CLEAN, True),
            (Outcome.EXHAUSTED, True),
            (Outcome.STALLED, True),
            (Outcome.REGRESSED, False),
            (Outcome.SUPPRESSED, False),
            (Outcome.UNPARSEABLE, False),
            (Outcome.DECLINED, False),
        ],
    )
    def test_only_honest_outcomes_may_be_written(self, outcome: Outcome, writable: bool):
        assert outcome.writable is writable


class TestTheCallersProjectIsNotTouched:
    """The difference between a tool you point at real work and one you do not."""

    def test_the_loop_writes_nothing_during_iteration(self, project: Path):
        before = sorted(p.relative_to(project) for p in project.rglob("*"))
        drive(spec_for(project), CARELESS, CLEAN)
        assert sorted(p.relative_to(project) for p in project.rglob("*")) == before

    def test_a_failed_run_leaves_the_project_identical(self, project: Path):
        before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
        drive(spec_for(project), CARELESS, GUTTED)
        assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before

    def test_committing_is_a_separate_decision(self, project: Path):
        """Accepting a run and writing it are two things, so one can happen without the other."""
        result, _ = drive(spec_for(project), CLEAN)
        assert not (project / "shop").exists()
        commit(result)
        assert (project / "shop" / "models.py").is_file()

    def test_a_committed_app_audits_the_way_the_loop_said_it_would(self, project: Path):
        """The loop's verdict is about a copy; this checks it transfers."""
        result, _ = drive(spec_for(project), CARELESS, CLEAN)
        commit(result)
        after = engine.run(project)
        assert in_app(after.findings, "shop") == ()


class TestReportingToTheModel:
    def test_only_the_generated_app_is_reported(self, project: Path):
        """A model asked for one app cannot fix the project's settings module."""
        result, _ = drive(spec_for(project), CARELESS, CLEAN)
        assert result.iterations[0].findings
        assert all(f.location.file.startswith("shop/") for f in result.iterations[0].findings)

    def test_findings_outside_the_app_are_withheld(self, project: Path):
        """The control: this project does emit findings in conf/settings.py, so
        the assertion above is not passing on an empty project."""
        whole = engine.run(project)
        assert any(f.location.file.startswith("conf/") for f in whole.findings)

    def test_the_summary_carries_a_fix(self, project: Path):
        result, _ = drive(spec_for(project), CARELESS, CLEAN)
        text = summarise(result.iterations[0].findings, "shop")
        assert "FIX:" in text

    def test_an_empty_summary_says_so_rather_than_being_blank(self):
        assert summarise([], "shop") == "no findings in the generated app"


BROKEN = {**CLEAN, "models_py": "class Order(models.Model:\n"}


class TestAStrayParenthesisDoesNotDiscardTheApp:
    """Measured against a real 7B model on loopback: the reply was idiomatic
    Django with one extra closing bracket on the ForeignKey line, and the run
    was refused entirely. A syntax error is the most repairable defect there
    is, and the repair machinery was already sitting there unused.

    The safety property is unchanged throughout: unparseable code is never
    written. What changes is whether the loop asks before giving up.
    """

    def test_a_broken_first_reply_is_sent_back_rather_than_refused(self, project: Path):
        result, _ = drive(spec_for(project), BROKEN, CLEAN)
        assert result.outcome is Outcome.CLEAN

    def test_the_model_is_told_what_was_wrong_with_it(self, project: Path):
        """A repair prompt that does not name the error is a re-roll."""
        _, provider = drive(spec_for(project), BROKEN, CLEAN)
        assert "models.py does not parse" in provider.prompts[1].user

    def test_the_broken_source_is_sent_back_with_the_error(self, project: Path):
        """Fixing it requires seeing it."""
        _, provider = drive(spec_for(project), BROKEN, CLEAN)
        assert "class Order(models.Model:" in provider.prompts[1].user

    # The controls. Retrying forever is a worse failure than refusing.

    def test_a_model_that_never_recovers_is_still_refused(self, project: Path):
        result, _ = drive(spec_for(project), BROKEN, BROKEN, BROKEN, BROKEN)
        assert result.outcome is Outcome.UNPARSEABLE

    def test_the_retry_budget_is_bounded(self, project: Path):
        """Three asks: the first attempt and two repairs. Not four, and not
        one per audit iteration -- a budget that renews is not a budget.
        """
        _, provider = drive(spec_for(project), *([BROKEN] * 6))
        assert len(provider.prompts) == MAX_SYNTAX_REPAIRS + 1

    def test_a_refused_run_is_still_never_written(self, project: Path):
        result, _ = drive(spec_for(project), BROKEN, BROKEN, BROKEN)
        with pytest.raises(ValueError, match="not written"):
            commit(result)

    def test_a_suppression_is_not_retried(self, project: Path):
        """The other refusal is not an accident. Asking a model that just
        tried to silence the auditor to try again invites a subtler attempt,
        so only a syntax error gets a second chance.
        """
        cheating = {**CLEAN, "views_py": CLEAN["views_py"] + "# djaudit: ignore\n"}
        result, provider = drive(spec_for(project), cheating, CLEAN)
        assert result.outcome is Outcome.SUPPRESSED
        assert len(provider.prompts) == 1
