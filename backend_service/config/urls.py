"""API-only URL configuration for the backend service."""

from cis_elements.api import router as cis_elements_router
from core.api import router as core_router
from django.urls import path
from jobs.api import artifact_router, input_router
from jobs.api import router as jobs_router
from ninja import NinjaAPI

api = NinjaAPI(title='Gene Family Backend API', version='0.1.0')
api.add_router('/core/', core_router)
api.add_router('/jobs', jobs_router)
api.add_router('/artifacts', artifact_router)
api.add_router('/inputs', input_router)
api.add_router('/cis-elements/', cis_elements_router)

urlpatterns = [
    path('api/', api.urls),
]
