FROM php:8.4-fpm-bookworm AS php

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

FROM nginx:1.28-alpine AS nginx
COPY --from=deployment nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=php /var/www/zentao/www/ /var/www/zentao/www/
