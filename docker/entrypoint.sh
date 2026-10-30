#!/bin/sh
set -e
unset DEPLOYER_PRIVATE_KEY
alembic upgrade head
exec python -m bot "$@"
