#!/bin/sh
set -eu

cd /srv/slow/apps/api
alembic -c alembic.ini upgrade head
exec "$@"
