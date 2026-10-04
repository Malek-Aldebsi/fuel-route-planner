from django.urls import path

from .views import RoutePlanView, map_page

urlpatterns = [
    path('api/route/', RoutePlanView.as_view(), name='route'),
    path('map/', map_page, name='map'),
]
