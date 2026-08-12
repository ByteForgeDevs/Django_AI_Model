from rest_framework.routers import DefaultRouter

from billing.views import InvoiceViewSet, LineItemViewSet

router = DefaultRouter()
router.register("invoices", InvoiceViewSet)
router.register("items", LineItemViewSet)
urlpatterns = router.urls
