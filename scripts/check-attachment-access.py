#!/usr/bin/env python3
"""Verify file authorization using two existing test fixtures and a signed-in browser cookie jar."""
import argparse
import hashlib
import json
from http.cookiejar import MozillaCookieJar
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, build_opener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="ZenTao origin, for example http://127.0.0.1:8080")
    parser.add_argument("cookies", help="Netscape-format cookie file for the test user")
    parser.add_argument("allowed_id", type=int)
    parser.add_argument("allowed_path", help="legacy /data/upload/ path for the authorized fixture")
    parser.add_argument("allowed_sha256", help="SHA-256 digest of the fixture contents")
    parser.add_argument("denied_id", type=int)
    parser.add_argument("denied_path", help="legacy /data/upload/ path for the inaccessible fixture")
    args = parser.parse_args()
    for path in (args.allowed_path, args.denied_path):
        if not path.startswith("/data/upload/"):
            parser.error("expected a local /data/upload/ fixture path")

    cookies = MozillaCookieJar(args.cookies)
    cookies.load(ignore_discard=True, ignore_expires=True)
    # curl writes session cookies with an expiry of 0 in Netscape cookie jars.
    for cookie in cookies:
        if cookie.expires == 0:
            cookie.expires = None
            cookie.discard = True
    cookies.clear_expired_cookies()
    browser = build_opener(ProxyHandler({}), HTTPCookieProcessor(cookies))

    def fetch(path):
        try:
            response = browser.open(args.base_url.rstrip("/") + path, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def api(method, file_id):
        return "/index.php?" + urlencode(dict(m="file", f=method, fileID=file_id, t="json"))

    for path in (args.allowed_path, api("read", args.allowed_id)):
        status, headers, body = fetch(path)
        if status != 200 or hashlib.sha256(body).hexdigest() != args.allowed_sha256:
            raise SystemExit("Authorized attachment did not return the expected content.")
        if "no-store" not in headers.get("Cache-Control", ""):
            raise SystemExit("Sensitive attachment response permits caching.")

    status, _, body = fetch(api("ajaxQuery", args.allowed_id))
    metadata = json.loads(body).get("file", {})
    if status != 200 or int(metadata.get("id", 0)) != args.allowed_id or "realPath" in metadata:
        raise SystemExit("Authorized attachment metadata is incorrect.")
    for path in (args.denied_path, api("read", args.denied_id), api("ajaxQuery", args.denied_id)):
        if fetch(path)[0] != 404:
            raise SystemExit("An inaccessible attachment was not concealed.")
    print("Authorized content, metadata, cache policy, and object-level denial passed.")


if __name__ == "__main__":
    main()
