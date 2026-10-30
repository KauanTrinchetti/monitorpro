from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    # Isso conecta as rotas do nosso monitor no sistema principal!
    path('', include('monitor.urls')), 
]