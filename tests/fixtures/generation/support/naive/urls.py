from rest_framework.routers import DefaultRouter

from support.views import ReplyViewSet, TicketViewSet

router = DefaultRouter()
router.register("tickets", TicketViewSet)
router.register("replies", ReplyViewSet)
urlpatterns = router.urls
