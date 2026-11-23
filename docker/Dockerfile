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
COPY --from=deployment --chmod=0644 php.ini /usr/local/etc/php/conf.d/zentao.ini
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
    install_extension() { \
        repository="$1"; tag="$2"; checksum="$3"; shift 3; \
        build_dir="$(mktemp -d)"; \
        curl --fail --silent --show-error --location --retry 2 \
            "https://codeload.github.com/$repository/tar.gz/refs/tags/$tag" -o "$build_dir/source.tgz"; \
        printf '%s  %s\n' "$checksum" "$build_dir/source.tgz" | sha256sum -c -; \
        tar -xzf "$build_dir/source.tgz" -C "$build_dir" --strip-components=1; \
        (cd "$build_dir" && phpize && ./configure "$@" && make -j2 && make install); \
        rm -rf "$build_dir"; \
    }; \
    case "$ZENTAO_CACHE_EXTENSION" in \
        none) exit 0 ;; \
        apcu) \
            install_extension krakjoe/apcu v5.1.28 0e60ba9d0fa4021c57a70c071f3d8f71de236275d084e560d5959fec4edd8c32; \
            docker-php-ext-enable apcu ;; \
        redis) \
            install_extension igbinary/igbinary 3.2.16 941f1cf2ccbecdc1c221dbfae9213439d334be5d490a2f3da2be31e8a00b0cdb; \
            docker-php-ext-enable igbinary; \
            install_extension phpredis/phpredis 6.3.0 cb8f81df1a275599e4f8ddcfec7e1f65ed1953e6f5673649149fd680ebff4cad \
                --enable-redis-igbinary; \
            docker-php-ext-enable redis; \
            php -r 'exit(extension_loaded("igbinary") && defined("Redis::SERIALIZER_IGBINARY") ? 0 : 1);' ;; \
        *) echo "Unsupported cache extension: $ZENTAO_CACHE_EXTENSION" >&2; exit 1 ;; \
    esac

FROM nginx:1.30.7-alpine-slim@sha256:8c950211a641ec5b23f265450517001f6bcca533666120ebe36cb4cff3d00a55 AS nginx
COPY --from=deployment nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=php-base /var/www/zentao/www/ /var/www/zentao/www/

FROM php AS php-hardened
RUN --mount=from=deployment,source=spreadsheet,target=/tmp/zentao-spreadsheet,ro \
    --mount=type=secret,id=apt_ca_bundle,mode=0444 \
    sh /tmp/zentao-spreadsheet/install.sh
COPY --from=deployment harden-router.php /usr/local/share/harden-router.php
COPY --from=deployment harden-files.php harden-log-urls.php /usr/local/share/
COPY --from=deployment --chmod=0644 private-upload.php /var/www/zentao/www/private-upload.php
COPY --from=deployment --chmod=0755 hardened-entrypoint.sh /usr/local/bin/zentao-hardened-entrypoint
RUN php -r 'exit(version_compare(PHP_VERSION, "8.4.27", ">=") ? 0 : 1);' \
    && php /usr/local/share/harden-router.php \
    && php /usr/local/share/harden-files.php \
    && php /usr/local/share/harden-log-urls.php \
    && php -l framework/base/router.class.php \
    && php -l module/file/model.php \
    && php -l module/file/control.php \
    && php -l module/common/model.php \
    && php -l www/private-upload.php \
    && rm /usr/local/share/harden-files.php /usr/local/share/harden-log-urls.php \
    && rm /usr/local/share/harden-router.php www/install.php www/upgrade.php \
    && mv tmp /usr/local/share/zentao-runtime \
    && install -d -o www-data -g www-data -m 0700 /var/lib/zentao/backup \
    && ln -s /var/lib/zentao/backup /usr/local/share/zentao-runtime/backup \
    && mkdir tmp \
    && apt-mark manual libfreetype6 libicu72 libjpeg62-turbo libpng16-16 libzip4 \
    && apt-get purge -y --auto-remove autoconf dpkg-dev g++ gcc libc6-dev make pkg-config re2c \
        libfreetype6-dev libicu-dev libjpeg62-turbo-dev libpng-dev libzip-dev \
    && php -r 'foreach (["bcmath", "gd", "intl", "mysqli", "pdo_mysql", "zip"] as $ext) if (!extension_loaded($ext)) exit(1);' \
    && printf 'error_log=/proc/self/fd/2\n' > /usr/local/etc/php/conf.d/zentao-hardened.ini \
    && printf '%s\n' '[www]' 'access.format = "%R %t %m %s %d"' > /usr/local/etc/php-fpm.d/zz-zentao.conf
ENTRYPOINT ["zentao-hardened-entrypoint"]
CMD ["php-fpm"]

FROM nginx AS nginx-hardened
COPY --from=deployment --chmod=0644 nginx.hardened.conf /etc/nginx/conf.d/default.conf
COPY --from=deployment --chmod=0644 fastcgi.hardened.conf /etc/nginx/zentao-fastcgi.conf
