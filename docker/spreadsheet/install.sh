#!/bin/sh
set -eu
bundle=$(dirname "$0")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ -s /run/secrets/apt_ca_bundle ]; then
    export CURL_CA_BUNDLE=/run/secrets/apt_ca_bundle COMPOSER_CAFILE=/run/secrets/apt_ca_bundle
fi
curl --fail --silent --show-error --location --retry 2 \
    https://github.com/composer/composer/releases/download/2.11.2/composer.phar -o "$work/composer.phar"
printf '%s  %s\n' dd158c0afd41d15fac89e1b4e47b5d42911fe316c3687cd5edac98319b25d028 "$work/composer.phar" | sha256sum -c -
cp "$bundle/composer.json" "$bundle/composer.lock" "$work/"
export COMPOSER_HOME="$work/home" COMPOSER_VENDOR_DIR=/opt/zentao-spreadsheet/vendor
php "$work/composer.phar" --working-dir="$work" --no-interaction --no-plugins --no-scripts \
    install --no-dev --prefer-dist --no-progress --no-cache
php "$bundle/activate.php" "$work"
php "$bundle/check.php"
