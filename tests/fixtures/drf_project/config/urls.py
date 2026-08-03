"""URLconf for the DRF recall fixture.

Nothing here is a defect. It exists because a view nobody routes is a view
nobody can reach, and every API rule is required to say so -- an unrouted class
is not an endpoint and must not be reported as one.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from support import views

router = DefaultRouter()
router.register("tickets", views.TicketViewSet, basename="ticket")
router.register("my-tickets", views.MyTicketViewSet, basename="my-ticket")
router.register("comments", views.CommentViewSet, basename="comment")
router.register("invoices", views.InvoiceViewSet, basename="invoice")

urlpatterns = [
    path("api/", include(router.urls)),
    path("api/tickets/<int:pk>/detail/", views.TicketDetailView.as_view()),
    path("api/profile/", views.ProfileView.as_view()),
    path("api/login/", views.LoginView.as_view()),
    path("api/tickets/export/", views.ticket_export),
]
