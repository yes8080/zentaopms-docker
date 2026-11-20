#!/bin/sh
set -eu

# Seed the ephemeral cache, log and application-generated PHP directories.
cp -a /usr/local/share/zentao-runtime/. /var/www/zentao/tmp/
exec docker-php-entrypoint "$@"
