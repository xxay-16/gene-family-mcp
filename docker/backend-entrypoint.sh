#!/bin/sh
set -eu

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  python manage.py migrate --noinput
fi

case "${1:-api}" in
  api)
    exec gunicorn config.wsgi:application \
      --bind 0.0.0.0:${PORT:-8000} \
      --workers ${WEB_CONCURRENCY:-2} \
      --timeout ${GUNICORN_TIMEOUT:-120}
    ;;
  worker)
    exec python manage.py qcluster
    ;;
  *)
    exec "$@"
    ;;
esac
