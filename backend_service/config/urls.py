"""API-only URL configuration for the backend service."""

from django.urls import path
from ninja import NinjaAPI

from cis_elements.api import router as cis_elements_router
from core.api import router as core_router
from jobs.api import artifact_router, router as jobs_router

api = NinjaAPI(title='Gene Family Backend API', version='0.1.0')
api.add_router('/core/', core_router)
api.add_router('/jobs', jobs_router)
api.add_router('/artifacts', artifact_router)
api.add_router('/cis-elements/', cis_elements_router)

urlpatterns = [
    path('api/', api.urls),
]
