from rest_framework.routers import DefaultRouter

from blog.views import CommentViewSet, PostViewSet

router = DefaultRouter()
router.register("posts", PostViewSet)
router.register("comments", CommentViewSet)
urlpatterns = router.urls
