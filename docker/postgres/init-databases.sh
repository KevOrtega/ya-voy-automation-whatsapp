#!/bin/sh
# Creates the Evolution API database next to the app database (POSTGRES_DB=yavoy).
# Runs only on the first start of an empty pg_data volume.
set -eu

for db in ${POSTGRES_EXTRA_DATABASES:-evolution}; do
  echo "Creating database '$db'"
  echo "SELECT 'CREATE DATABASE \"$db\" OWNER \"$POSTGRES_USER\"' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db')\\gexec" \
    | psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
done
