import os


BACKEND_API_URL = os.getenv(
    'GENE_FAMILY_BACKEND_URL',
    'http://127.0.0.1:8000/api',
).rstrip('/')
BACKEND_TIMEOUT = float(os.getenv('GENE_FAMILY_BACKEND_TIMEOUT', '30'))
