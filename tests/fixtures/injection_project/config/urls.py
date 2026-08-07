"""URLconf for the injection recall fixture.

Every view is routed, defects and controls alike. An unrouted view is not an
endpoint, and a control the rule never looks at passes by being invisible --
which is indistinguishable from the rule being broken.
"""

from django.urls import path

from shop import controls, views

urlpatterns = [
    # The defects, one per DJI rule.
    path("search/", views.product_search),
    path("sku/", views.product_by_sku),
    path("over/", views.products_over),
    path("ranked/", views.products_ranked),
    path("filter/", views.product_filter),
    path("sorted/", views.product_sorted),
    path("cart/restore/", views.restore_cart),
    path("convert/", views.convert_image),
    path("feed/", views.fetch_feed),
    path("after-login/", views.after_login),
    path("banner/", views.product_banner),
    path("invoice/", views.download_invoice),
    # The same sinks, reached safely.
    path("c/search/", controls.product_search),
    path("c/sku/", controls.product_by_sku),
    path("c/over/", controls.products_over),
    path("c/ranked/", controls.products_ranked),
    path("c/filter/", controls.product_filter),
    path("c/sorted/", controls.product_sorted),
    path("c/sorted-checked/", controls.product_sorted_checked),
    path("c/cart/restore/", controls.restore_cart),
    path("c/convert/", controls.convert_image),
    path("c/feed/", controls.fetch_feed),
    path("c/after-login/", controls.after_login),
    path("c/banner/", controls.product_banner),
    path("c/invoice/", controls.download_invoice),
]
