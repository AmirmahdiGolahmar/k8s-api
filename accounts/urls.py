from django.urls import path

from . import views

urlpatterns = [
    path('csrf/', views.csrf, name='auth-csrf'),
    path('login/', views.login_view, name='auth-login'),
    path('register/', views.register_view, name='auth-register'),
    path('logout/', views.logout_view, name='auth-logout'),
    path('me/', views.me, name='auth-me'),
    path('users/', views.list_users, name='auth-users'),
]
