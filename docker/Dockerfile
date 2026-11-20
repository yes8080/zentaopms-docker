FROM php:8.4-fpm-bookworm AS php-base

RUN --mount=type=secret,id=apt_ca_bundle,mode=0444 \
    set -- \
    && if [ -s /run/secrets/apt_ca_bundle ]; then set -- -o Acquire::https::CaInfo=/run/secrets/apt_ca_bundle; fi \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get "$@" update \
    && apt-get "$@" install -y --no-install-recommends libfreetype6-dev libicu-dev libjpeg62-turbo-dev libpng-dev libzip-dev \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install -j 2 bcmath gd intl mysqli opcache pdo_mysql zip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /var/www/zentao
COPY --chown=root:root . .
COPY --from=deployment php.ini /usr/local/etc/php/conf.d/zentao.ini
RUN chmod -R a+rX /var/www/zentao \
    && mkdir -p /var/lib/zentao/config \
    && touch /var/lib/zentao/config/my.php \
    && chmod 0600 /var/lib/zentao/config/my.php \
    && ln -s /var/lib/zentao/config/my.php config/my.php \
    && chown -R www-data:www-data /var/lib/zentao/config config tmp www www/data

FROM php-base AS php
ARG ZENTAO_CACHE_EXTENSION=none
RUN --mount=type=secret,id=apt_ca_bundle,mode=0444 \
    set -eu; \
    case "$ZENTAO_CACHE_EXTENSION" in \
        none) exit 0 ;; \
        apcu) package=apcu-5.1.28; checksum=ca9c1820810a168786f8048a4c3f8c9e3fd941407ad1553259fb2e30b5f057bf ;; \
        redis) package=redis-6.3.0; checksum=0d5141f634bd1db6c1ddcda053d25ecf2c4fc1c395430d534fd3f8d51dd7f0b5 ;; \
        *) echo "Unsupported cache extension: $ZENTAO_CACHE_EXTENSION" >&2; exit 1 ;; \
    esac; \
    set --; \
    if [ -s /run/secrets/apt_ca_bundle ]; then set -- --cacert /run/secrets/apt_ca_bundle; fi; \
    curl "$@" --fail --silent --show-error --location "https://pecl.php.net/get/$package.tgz" -o "/tmp/$package.tgz"; \
    printf '%s  %s\n' "$checksum" "/tmp/$package.tgz" | sha256sum -c -; \
    pecl install "/tmp/$package.tgz" </dev/null; \
    docker-php-ext-enable "$ZENTAO_CACHE_EXTENSION"; \
    rm -rf "/tmp/$package.tgz" /tmp/pear

FROM nginx:1.28-alpine AS nginx
COPY --from=deployment nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=php-base /var/www/zentao/www/ /var/www/zentao/www/
