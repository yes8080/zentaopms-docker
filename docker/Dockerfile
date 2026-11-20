FROM php:8.4-fpm-bookworm@sha256:e4b1eec29a5be22428aa93fb43da75a0e39006e403da0c3e93631dde92f067df AS php-base

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
    if [ -s /run/secrets/apt_ca_bundle ]; then export CURL_CA_BUNDLE=/run/secrets/apt_ca_bundle; fi; \
    install_pecl() { \
        package="$1"; checksum="$2"; shift 2; \
        archive="/tmp/$package.tgz"; \
        curl --fail --silent --show-error --location "https://pecl.php.net/get/$package.tgz" -o "$archive"; \
        printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c -; \
        pecl install "$@" "$archive" </dev/null; \
        rm -f "$archive"; \
    }; \
    case "$ZENTAO_CACHE_EXTENSION" in \
        none) exit 0 ;; \
        apcu) \
            install_pecl apcu-5.1.28 ca9c1820810a168786f8048a4c3f8c9e3fd941407ad1553259fb2e30b5f057bf; \
            docker-php-ext-enable apcu ;; \
        redis) \
            install_pecl igbinary-3.2.16 8bf25d465abc7973d9e2c9a3039a5f8eea635b23bc1477017ff3999ff95836da; \
            docker-php-ext-enable igbinary; \
            install_pecl redis-6.3.0 0d5141f634bd1db6c1ddcda053d25ecf2c4fc1c395430d534fd3f8d51dd7f0b5 \
                --configureoptions 'enable-redis-igbinary="yes"'; \
            docker-php-ext-enable redis; \
            php -r 'exit(extension_loaded("igbinary") && defined("Redis::SERIALIZER_IGBINARY") ? 0 : 1);' ;; \
        *) echo "Unsupported cache extension: $ZENTAO_CACHE_EXTENSION" >&2; exit 1 ;; \
    esac; \
    rm -rf /tmp/pear

FROM nginx:1.30.7-alpine@sha256:caf4e1e032a20f9c3c5e28fc7f25ffd768ad8677b4975c54193075997b3a97e3 AS nginx
COPY --from=deployment nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=php-base /var/www/zentao/www/ /var/www/zentao/www/

FROM php AS php-hardened
COPY --from=deployment harden-router.php /usr/local/share/harden-router.php
COPY --from=deployment --chmod=0755 hardened-entrypoint.sh /usr/local/bin/zentao-hardened-entrypoint
RUN php -r 'exit(version_compare(PHP_VERSION, "8.4.27", ">=") ? 0 : 1);' \
    && php /usr/local/share/harden-router.php \
    && php -l framework/base/router.class.php \
    && rm /usr/local/share/harden-router.php www/install.php www/upgrade.php \
    && mv tmp /usr/local/share/zentao-runtime \
    && mkdir tmp \
    && apt-mark manual libfreetype6 libicu72 libjpeg62-turbo libpng16-16 libzip4 \
    && apt-get purge -y --auto-remove autoconf dpkg-dev g++ gcc libc6-dev make pkg-config re2c \
        libfreetype6-dev libicu-dev libjpeg62-turbo-dev libpng-dev libzip-dev \
    && php -r 'foreach (["bcmath", "gd", "intl", "mysqli", "pdo_mysql", "zip"] as $ext) if (!extension_loaded($ext)) exit(1);' \
    && printf 'log_errors=On\nerror_log=/proc/self/fd/2\n' > /usr/local/etc/php/conf.d/zentao-hardened.ini
ENTRYPOINT ["zentao-hardened-entrypoint"]
CMD ["php-fpm"]

FROM nginx AS nginx-hardened
COPY --from=deployment nginx.hardened.conf /etc/nginx/conf.d/default.conf
