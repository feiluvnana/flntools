# loi

Phase 4, step 5. A flnd source for LOIBUS (loix.cc), ported from `flnkit/projects/loix`. It uses the [engine](engine.md) and the step 2 modules; it is only the site's logic.

```sh
flnd download loi:5930
flnd download loi:https://loix.cc/lois/5930        # a post URL works too
flnd download loi:5930 ins:example_user           # mixed with other sources, in parallel
flnd download loi:5930 --from photo,archive --unpack    # also the VIP archives, unpacked
```

## What is ported, and what isn't

The flnkit client is about 1,840 lines: `api.py` 782, `get.py` 411, `tui.py` 326, `__main__.py` 253, and `settings.py` 70. The source ports only the "get one post" path, which is about 200–250 lines.

| loix feature | In the `loi` source |
| --- | --- |
| request signing (`x-request-id`, `x-request-signature`, `x-signature-timestamp`), Bearer token from the `access-token` cookie | yes, through `Source.sign` |
| post detail (`lois/<id>`), download links (`user/loi/download_v6/<id>`), account (`user/info`) | yes |
| AES-128-CBC decryption of protected fields | yes, through `crypto.decrypt_aes_cbc` |
| `info.md` with the post's fields, unzip password included | yes |
| preview images | yes |
| full archive (VIP): mirrors, 7z/zip sniffing, password unpack, keep or delete the archive | yes |
| daily VIP quota check | yes, through `session.state` (a `kv` table) with `ttl` |
| done record | yes; it is the engine's history |
| feeds, search, authors, tags, `posts --all` | no, because flnd takes a post id. They can come later as a `flnd loi` command group if wanted |
| the full-screen TUI | no; full-screen widgets are [out of scope](../README.md#out-of-scope) |
| `config.toml` settings and the response cache | no. flnd's conventions apply (`.flnd/`), and one post needs no cache |

**No browser.** LOIBUS blocks browser scraping, so the source never calls `session.open`. The engine never starts Chromium for `loi` targets.

## The source

```python
class Loibus(Source):
    code = "loi"
    name = "LOIBUS"
    domains = ("loix.cc", "moegoat.com")
    needs = ("access-token",)                 # without it: images and info only, no archives
    categories = ("photo", "archive")         # its content is naturally typed, so the types are the categories
    default_categories = ("photo",)
    api = "https://api.moegoat.com/api/"

    def headers(self, cookies):               # the site's referer and origin, and the Bearer token
        token = decode_token(cookies.get("access-token", ""))
        return {"referer": SITE, "origin": SITE.rstrip("/"), **({"authorization": f"Bearer {token}"} if token else {})}

    def sign(self, request):                  # runs on every attempt, retries included
        if request.url.host != API_HOST:      # images and mirrors: no signature, and the token is dropped
            request.headers.pop("authorization", None)
            return request
        return sign_request(request, now_ms=_clock_ms())

    def resolve(self, text) -> Target:        # "5930" or a loix.cc post URL -> kind "post", value "5930"
        ...


    def run(self, session):
        post = session.fetch_json(f"{API}lois/{session.target.value}").get("data")   # FetchError when missing
        day = posted_on(post)
        if not session.wants(date=day):
            return
        session.folder = folder_of(post)                       # author / "<id> <title>"
        links = self._links(session, post) if session.wants(category="archive") else None
        session.write("info.md", info_md(post, links))
        for number, url in enumerate(post["images"], start=1):
            session.save(url, image_name(number, url), category="photo", date=day, folder=f"{session.folder}/images")
        if links is None:
            return
        queued = [session.save(mirrors, name, category="archive", date=day, password=post["open_key"] or None)
                  for name, mirrors in group_mirrors(links)]
        if any(queued):
            count_download(session, post["id"])
```

`_links` warns and gives None without a token, when the quota is used up, or when `download_v6` is refused or empty; otherwise it decrypts every `sv_list[].sv_url` and the Baidu link and key.

What the live site showed, and the code follows:
- **`open_key` is plain text**, not encrypted, so it is the password as it is.
- **Each `sv_list` entry is a mirror**: the two VIP lines serve the same file. `group_mirrors` groups them by the URL's file name (`loi-<hash>.7z`), which is also the archive's name, so the file is saved once and its extension is the server's. loix grouped by server name, which would have saved one archive per line.
- **`download_v6` counts as a download** on the site (it shows up in `user/mydownloaded`), so it is called only when `--from` takes `archive`, after the quota check. With the default categories, the source makes one request, `lois/<id>`.
- **The quota** is `session.state["quota"] = {"day": "<today>", "posts": [<ids>]}`, against `VIP_DAILY_LIMITS[vip_id]` (twice for SVIP) from `user/info`. A post already counted today is free again, a post is counted only when one of its archives was queued (not when all were skipped), and the record of an earlier day no longer counts. One overwritten key needs no `ttl`, which `Session.state` (a `MutableMapping`) doesn't have.
- `info.md` is written on every run, so its stats and links stay current; it has no history row.

The `sign_request`, `decode_token`, `decrypt_field`, `info_md` and `folder_of` helpers move from `loix/api.py` and `loix/get.py` with their logic unchanged. They use `crypto.hash_sha256` and `crypto.decrypt_aes_cbc` instead of flnkit's `crypto`, and the stdlib `secrets` instead of `crypto_random` / `crypto_choice` (`sign_request` takes `pick` and `noise` for tests). `info.md` always holds the unzip password, and the download links when archives were asked for.

**Unpacking.** `--unpack` unpacks each archive into a folder of its name with the post's password. The engine also unpacks an archive an earlier run saved without `--unpack`, once its folder is missing ([engine › Download options](engine.md#download-options)), so `--from archive --unpack` after `--from all` needs no new download.

## Engine features it uses

`Source.sign`, `Source.resolve`, the default login methods, `--from archive` through `session.wants`, `session.write`, mirror lists and `password=` on `session.save`, `session.fetch_json(..., soft=True)`, and `session.state` are all in the [engine](engine.md).

## Login

loi uses the engine's default login ([engine › Login](engine.md#login)): `flnd login loi` reads the `loix.cc` cookies from your browser or a cookie file, checks that `access-token` is there (`needs = ("access-token",)`), and saves them to `.flnd/cookies/loi.txt`. `headers()` turns that cookie into the `Bearer` token on every request. There is no login code in the source.

Without a login, loi still downloads a post's info and preview images. Archives (`--from archive`) need the token, and say so.

## Tests

- `sign_request` against loix's existing test vectors, with a fixed clock and `pick`.
- `decrypt_field` against a known ciphertext.
- `run` through `FakeSession`:
  - a post without a token: info and images only;
  - with a token and `--from archive`: mirrors grouped, `password=` given the decrypted password;
  - the default categories: no archive requested, and `links` never fetched;
  - quota used up: archives skipped with a warning, and `links` not fetched; a post counted today is free;
  - refused links, a post outside `--since`/`--until`, and a missing post.
- The engine end to end with `httpx.MockTransport`:
  - signatures are fresh on a retried request;
  - a 7z archive served as `.zip` is unpacked;
  - a 403 on links is soft.
- `loi:https://loix.cc/lois/5930` resolves to `5930`.
- **Login** through the engine's default methods: a fake browser cookie store and a cookie file, with `access-token` found and missing.
