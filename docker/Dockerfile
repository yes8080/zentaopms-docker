FROM php:8.4-fpm-trixie@sha256:89aa7c69dedff16e4076f955578547b62566b7f9aa6a0caa6b34b554490be4f0 AS php-runtime

RUN --mount=type=secret,id=apt_ca_bundle,mode=0444 \
    set -- \
    && if [ -s /run/secrets/apt_ca_bundle ]; then set -- -o Acquire::https::CaInfo=/run/secrets/apt_ca_bundle; fi \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get "$@" --error-on=any update \
    && apt-get "$@" -y upgrade \
    && apt-get "$@" install -y --no-install-recommends libfreetype6-dev libicu-dev libjpeg62-turbo-dev libpng-dev libzip-dev \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install -j 2 bcmath gd intl mysqli opcache pdo_mysql zip \
    && rm -rf /var/lib/apt/lists/*

ARG ZENTAO_CACHE_EXTENSION=none
RUN --mount=type=secret,id=apt_ca_bundle,mode=0444 \
    set -eu; \
    if [ -s /run/secrets/apt_ca_bundle ]; then export CURL_CA_BUNDLE=/run/secrets/apt_ca_bundle; fi; \
    install_extension() { \
        name="$1"; source="$2"; checksum="$3"; shift 3; \
        archive="/tmp/$name.tar.gz"; src="/tmp/$name-src"; \
        curl --fail --silent --show-error --location "$source" -o "$archive"; \
        printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c -; \
        mkdir "$src"; tar -xzf "$archive" -C "$src" --strip-components=1; \
        (cd "$src" && phpize && ./configure "$@" && make -j2 && make install); \
        docker-php-ext-enable "$name"; \
        rm -rf "$archive" "$src"; \
    }; \
    case "$ZENTAO_CACHE_EXTENSION" in \
        none) exit 0 ;; \
        apcu) \
            install_extension apcu https://codeload.github.com/krakjoe/apcu/tar.gz/refs/tags/v5.1.28 0e60ba9d0fa4021c57a70c071f3d8f71de236275d084e560d5959fec4edd8c32 ;; \
        redis) \
            install_extension igbinary https://codeload.github.com/igbinary/igbinary/tar.gz/refs/tags/3.2.16 941f1cf2ccbecdc1c221dbfae9213439d334be5d490a2f3da2be31e8a00b0cdb; \
            install_extension redis https://codeload.github.com/phpredis/phpredis/tar.gz/refs/tags/6.3.0 cb8f81df1a275599e4f8ddcfec7e1f65ed1953e6f5673649149fd680ebff4cad --enable-redis-igbinary; \
            php -r 'exit(extension_loaded("igbinary") && defined("Redis::SERIALIZER_IGBINARY") ? 0 : 1);' ;; \
        *) echo "Unsupported cache extension: $ZENTAO_CACHE_EXTENSION" >&2; exit 1 ;; \
    esac; \
    rm -rf /tmp/pear

FROM php-runtime AS php-base
WORKDIR /var/www/zentao
RUN --mount=type=bind,target=/run/zentao-src \
    --mount=type=bind,from=deployment,source=php.ini,target=/run/zentao.ini \
    cp -R /run/zentao-src/. . \
    && install -m 0644 /run/zentao.ini /usr/local/etc/php/conf.d/zentao.ini \
    && chmod -R a+rX /var/www/zentao \
    && mkdir -p /var/lib/zentao/config \
    && install -d -o www-data -g www-data -m 0700 tmp/backup \
    && touch /var/lib/zentao/config/my.php \
    && chmod 0600 /var/lib/zentao/config/my.php \
    && ln -s /var/lib/zentao/config/my.php config/my.php \
    && chown -R www-data:www-data /var/lib/zentao/config config tmp www www/data

FROM php-base AS php

FROM nginx:1.30.7-alpine-slim@sha256:8c950211a641ec5b23f265450517001f6bcca533666120ebe36cb4cff3d00a55 AS nginx
COPY --chmod=0644 --from=deployment nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=php-base /var/www/zentao/www/ /var/www/zentao/www/

FROM php AS php-hardened
RUN --mount=type=bind,from=deployment,target=/run/deployment \
    install -m 0755 /run/deployment/hardened-entrypoint.sh /usr/local/bin/zentao-hardened-entrypoint \
    && php -r 'exit(version_compare(PHP_VERSION, "8.4.27", ">=") ? 0 : 1);' \
    && php /run/deployment/harden-router.php \
    && php -l framework/base/router.class.php \
    && rm www/install.php www/upgrade.php \
    && mv tmp /usr/local/share/zentao-runtime \
    && install -d -o www-data -g www-data -m 0700 tmp/backup \
    && apt-mark manual libfreetype6 libicu76 libjpeg62-turbo libpng16-16t64 libzip5 \
    && apt-get purge -y --auto-remove autoconf dpkg-dev g++ gcc libc6-dev make pkg-config re2c \
        libfreetype6-dev libicu-dev libjpeg62-turbo-dev libpng-dev libzip-dev \
    && php -r 'foreach (["bcmath", "gd", "intl", "mysqli", "pdo_mysql", "zip"] as $ext) if (!extension_loaded($ext)) exit(1);' \
    && printf 'log_errors=On\nerror_log=/proc/self/fd/2\n' > /usr/local/etc/php/conf.d/zentao-hardened.ini
ENTRYPOINT ["zentao-hardened-entrypoint"]
CMD ["php-fpm"]

FROM nginx AS nginx-hardened
ARG ZENTAO_REFRESH_OS_PACKAGES=0
RUN --mount=type=secret,id=apt_ca_bundle,mode=0444 \
    set -eu; \
    sed -i '/^user[[:space:]]/d' /etc/nginx/nginx.conf; \
    case "$ZENTAO_REFRESH_OS_PACKAGES" in \
        0) exit 0 ;; \
        1) ;; \
        *) echo "ZENTAO_REFRESH_OS_PACKAGES must be 0 or 1" >&2; exit 1 ;; \
    esac; \
    if [ -s /run/secrets/apt_ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/apt_ca_bundle; fi; \
    apk --no-cache update; \
    apk --no-cache upgrade
COPY --chmod=0644 --from=deployment nginx.hardened.conf /etc/nginx/conf.d/default.conf
