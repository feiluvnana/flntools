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

    def headers(self, cookies):
        token = decode_token(cookies.get("access-token", ""))
        return {"authorization": f"Bearer {token}"} if token else {}

    def sign(self, request):                  # runs on every attempt, retries included
        return sign_request(request, now_ms=clock_ms())

    def resolve(self, text) -> Target:        # "5930" or a loix.cc post URL -> kind "post", value "5930"
        ...


    def run(self, session):
        post = session.select(session.fetch_json(f"{self.api}lois/{session.target.value}")).jmespath("data").first
        session.folder = folder_of(post)                       # author / "<id> <title>"
        links = session.fetch_json(f"{self.api}user/loi/download_v6/{post['id']}", soft=True)
        session.write("info.md", info_md(post, links))
        for url in post["images"]:
            session.save(url, image_name(url), category="photo", date=post_date(post))
        if links and session.wants(category="archive") and quota_left(session):
            for name, mirrors in group_mirrors(links):
                session.save(mirrors, name, category="archive", date=post_date(post),
                             password=decrypt_field(post["open_key"]))
```

The `sign_request`, `decode_token`, `decrypt_field`, `info_md` and `folder_of` helpers move from `loix/api.py` and `loix/get.py` with their logic unchanged. They use `crypto.hash_sha256` and `crypto.decrypt_aes_cbc` instead of flnkit's `crypto`, and the stdlib `secrets` instead of `crypto_random` / `crypto_choice`.

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
  - the default categories: no archive requested, and `links` never fetched for archives;
  - quota used up: archives skipped with a warning.
- The engine end to end with `httpx.MockTransport`:
  - signatures are fresh on a retried request;
  - a 7z archive served as `.zip` is unpacked;
  - a 403 on links is soft.
- `loi:https://loix.cc/lois/5930` resolves to `5930`.
- **Login** through the engine's default methods: a fake browser cookie store and a cookie file, with `access-token` found and missing.
