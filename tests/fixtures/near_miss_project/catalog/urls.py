"""Router registrations for the near-miss API.

Routed on purpose: an endpoint nothing routes is not evaluated at all, so a
view that is only correct because it is unreachable would prove nothing.
"""

from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("bookmarks", views.BookmarkViewSet, basename="bookmark")
router.register("invoices", views.InvoiceViewSet, basename="invoice")
router.register("subscriptions", views.SubscriptionViewSet, basename="subscription")
router.register("audit", views.AuditEntryViewSet, basename="audit")
router.register("audit-recent", views.RecentAuditViewSet, basename="audit-recent")

urlpatterns = [
    path("accounts/<int:pk>/", views.AccountDetailView.as_view()),
    path("login/", views.LoginView.as_view()),
    path("status/", views.StatusView.as_view()),
    path("password/change/", views.password_change),
    *router.urls,
]
