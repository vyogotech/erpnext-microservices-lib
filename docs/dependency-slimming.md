# Dependency slimming

The service image is built from `pip install frappe` + `pip install erpnext`
(`Containerfile`). Frappe's dependency list assumes a **full Desk install** — PDF
printing, Excel import, 2FA QR codes, full-text search, an interactive console,
Google integrations. A headless microservice uses almost none of it.

Two build steps remove what a service cannot reach. Both are **measured, not
guessed**, and both are re-verifiable with `scripts/check_pruned_deps.py`.

| | Before | After |
|---|---|---|
| Image size | 948 MB | **433 MB** (−54%) |
| Python distributions | 174 | 126 |
| of which packages | 636 MB | 254 MB |

This library declares **12** dependencies. Everything removed below came from
Frappe, not from here.

---

## 1. Installing only the dependencies a service needs

Frappe declares ~75 dependencies in `pyproject.toml`. It ships **no
`requirements.txt`**, so there is nothing to override — and two entries are git
URLs (`PyPika`, `gunicorn`) that must survive verbatim.

Rather than install all of them and uninstall the unused ones afterwards — which
wastes the download and makes the build log claim we ship packages we do not —
the Containerfile installs frappe and erpnext with `--no-deps`, then asks their
*installed metadata* what they declare and installs that minus an exclusion list:

```dockerfile
pip install --no-deps git+.../frappe@${FRAPPE_VERSION}
pip install --no-deps git+.../erpnext@${ERPNEXT_VERSION}
python scripts/resolve_frappe_deps.py > /tmp/frappe-deps.txt
pip install -r /tmp/frappe-deps.txt
```

Because the list comes from Frappe's own metadata at build time, **a version bump
picks up new dependencies automatically**. Only `scripts/excluded_packages.txt` is
hand-maintained — the part that needs human judgement.

Two supporting files:

| File | Purpose |
|---|---|
| `scripts/excluded_packages.txt` | The exclusion list. Single source of truth, grouped by capability |
| `scripts/extra_packages.txt` | Packages Frappe/ERPNext **import without declaring**. Under the old flow these arrived by accident as someone else's transitive dependency; `--no-deps` makes the gap visible |

> `extra_packages.txt` currently holds `lxml`. ERPNext's EDI modules import it
> directly but nothing declares it — it used to arrive via `premailer`, which is
> excluded. `check_pruned_deps.py` is what surfaced this. When a package turns
> out to be needed, add it there; never widen the exclusion list to fix it.

### How the exclusion list was produced

Boot the service against a live site and dump `sys.modules`. A real boot loads
**205 top-level modules**. Anything absent from that set is a candidate:

```python
frappe.init(...); frappe.connect()
# import the library, load a doc, resolve a doctype's meta
tops = {m.split(".")[0] for m in sys.modules}
```

Notably `frappe/app.py` — Frappe's own WSGI entry, which top-level imports
`babel`, `sentry-sdk` and `num2words` — **never loads**, because services run
Flask.

### Excluded

`duckdb` (imported nowhere in Frappe at all) · `google-api-python-client` and its
auth stack · the `ipython` tree · the WeasyPrint tree · `GitPython` · `zxcvbn` ·
`ldap3` · `pycountry` · `posthog` · `vobject` · `rauth` · `premailer` · `Faker` ·
`PyQRCode` · `websockets`

### Deliberately kept (installed)

| Package(s) | Why |
|---|---|
| `Pillow`, `RestrictedPython`, `croniter`, `passlib`, `pdfkit`, `pypdf`, `chardet`, `pyotp`, `mysqlclient`, `PyMySQL` | Present in the boot trace |
| `babel`, `phonenumbers`, `num2words`, `markdown2`, `sentry-sdk` | Imported **inside functions**, so removing them fails at feature-use rather than at boot — a worse failure mode to introduce without exercising those paths. `num2words` backs `money_in_words` on invoices |
| `psycopg2-binary` | `Containerfile.postgres` needs it |
| `Whoosh`, `xlrd`, `openpyxl`, `xlsxwriter`, `et-xmlfile` | **See the warning below** |

> **Boot tracing alone is not sufficient.** `Whoosh` and the Excel packages look
> obviously Desk-only — absent from the boot trace, each imported by a single
> Frappe module. Removing them broke **40 modules**, including core ERPNext
> controllers (`sales_order`, `stock_entry`, `work_order`) and
> `frappe.desk.query_report`. They are imported transitively from places static
> analysis does not reveal. This is what `check_pruned_deps.py` exists to catch.

---

## 2. Removed translation catalogues

`frappe/locale` is **46 MB of a 73 MB package** — 33 `.po` files plus one `.pot`.
ERPNext ships its own. `frappe/public` and `erpnext/public` (browser assets) go
too; a headless service serves no UI.

### Nothing reads them today

The library never sets `frappe.local.lang`, and `lang` is never written to
`site_config.json`. Frappe therefore falls back to:

```python
local.lang = local.conf.lang or "en"     # frappe/__init__.py
```

on every request, and serves English regardless of which `.po` files are
present. **Removing them is a no-op, not a trade-off.**

### Currency is not affected

`locale/` contains gettext UI strings only — no currency, locale or number
formatting data. All three currency concerns read from the database:

| Concern | Source |
|---|---|
| Conversion (exchange rates) | ERPNext `Currency Exchange` doctype / external API |
| Formatting (symbol, separators, precision) | `Currency` doctype + `currency_precision` default |
| Amount in words | `Currency.fraction` / `fraction_units` + the `num2words` package |

`money_in_words` wraps its output in `frappe._()`, so the surrounding *words*
would be English — but they already are, per above. The number, symbol, fraction
handling and rate are untouched.

### If server-side translation is needed later

Shipping `.po` files back is **not sufficient on its own**. In order:

1. **Set the language per request.** In `secure_route`, where `g.tenant_id` is
   set after authentication, also set `frappe.local.lang` from the user record or
   the `Accept-Language` header. It must run *after* `_restore_frappe_local()`,
   which resets `frappe.local` on every request. Without this, `.po` files do
   nothing — which is the situation the removal documents.
2. **Reinstate only the languages actually sold into** (~1.4 MB each), not all 33.

The recommended alternative is to translate at the edge: services return a stable
error code plus params, and the client — which owns the user's locale and already
has an i18n catalogue for its own UI — renders the text. `secure_route`'s error
handling (where `TenantAccessError` and `MASK_PERMISSION_ERRORS` are handled) is
the natural place to map ERPNext exceptions to codes, passing unmapped ones
through as an English `message` exactly as happens today.

---

## 3. Removed the `git` binary from the runtime stage

Worth **104 MB**. Only three Frappe call sites use it, all dev/reporting:
`build.py` (asset building) and `utils/change_log.py` (the About-dialog version
lookup). Both shell out to `git -C ../apps/<app>` — a path that does not exist in
this image, so git was **already non-functional** here. `GitPython` is pruned for
the same reason.

The builder stage still needs it, for `pip install git+https://...`.

### Two packages that look removable but are not

| Package | Why it stays |
|---|---|
| `babel` (27.5 MB) | Not frontend-only. `frappe/utils/data.py` uses it in `format_date`, `format_time`, `format_datetime`, `global_date_format`, `pretty_date`, `comma_or` and **`comma_and`** — the last builds ERPNext validation messages (`Items 'A', 'B' and 'C' ...`). Its `from babel import Locale` sits *outside* the surrounding `try`, so an `ImportError` propagates and a routine validation error becomes a crash |
| `markdown2` (<1.6 MB) | Most callers are website rendering, `printview` and `website_search`, but `frappe/email/__init__.py` reaches it via `frappe.utils.markdown()`. Under 1.6 MB is not worth a broken email path |

---

## Verifying a build

Two checks, and they cover different things.

### 1. Import-level: `check_pruned_deps.py`

**CI runs this before the image is pushed** (`build` job). Run it by hand against any image, and especially after a Frappe upgrade — a
new release can add a top-level import and silently invalidate the prune list:

```bash
podman run --rm --entrypoint /opt/venv/bin/python \
    <image> /app/scripts/check_pruned_deps.py
```

It boots the service, then walk-imports every Frappe and ERPNext module, exiting
non-zero if anything breaks **outside** the features intentionally dropped. The
allowed-failure list is in the script, with a justification for each entry.

Modules expected to fail, because their whole purpose is a pruned feature:
Google integrations, LDAP settings, telemetry, WeasyPrint, password strength,
boilerplate, `frappe.commands`, `frappe.gettext`, and `frappe.patches` /
`frappe.utils.install` (migrate-time only — the service image has no bench, and
schema sync goes through `presync_service_doctypes`).

### 2. Runtime: the integration suite, from inside the image

`scripts/run_integration_tests.sh` runs `tests/integration/` inside the
**central-site bench container**, which has the full unpruned Frappe install. It
validates the library, but proves nothing about the image being shipped — a
broken prune still shows all green there.

`scripts/run_service_image_integration.sh` closes that gap. It pairs the built
service image with central-site's MariaDB and Redis and runs the same suite from
inside the image, exactly as a microservice connects in production:

```bash
SERVICE_IMAGE=localhost/frappe-microservice-lib:latest \
    ./scripts/run_service_image_integration.sh
```

Expected: **12 passed, 9 skipped, 0 failed**.

The 9 skips are all of `test_tenant_id_stamping.py`. Every test in that module
asserts on tenancy_app's `"*"` before_insert hook, which service images
deliberately do not ship — the library's `get_valid_dict` patch is the
replacement. Its guard checks both that the app is *recorded* as installed
(`frappe.get_installed_apps()`, a database read) and that this process can
*import* it. Running a service image against a central-site database is what
makes those two diverge; checking only the first was a latent bug, invisible for
as long as the suite only ever ran inside central-site.

> **One suite per stack.** Do not run both scripts against the same live
> containers. Both mutate the shared site database, and each leaves state that
> breaks the other — verified in both directions. Each script tears down with
> `down -v` so every invocation starts clean.

### Why import-level checking is not enough on its own

`check_pruned_deps.py` catches anything that fails to *import*. It cannot catch a
package that imports fine but is needed at call time — which is precisely the
risk class behind keeping `babel`, `phonenumbers`, `num2words`, `markdown2` and
`sentry-sdk`. Only running real requests against the image covers those.

---

## Package inventories

A stock `pip install frappe erpnext` produces **174 distributions** (636.7 MB).
The slimmed service image keeps 126 of them (409.8 MB):

| Status | Count | Size |
|---|---|---|
| `shipped` | 126 | 409.8 MB |
| `excluded` — named in `scripts/excluded_packages.txt` | 39 | 218.9 MB |
| `dropped (transitive)` — only required by something excluded | 9 | 8.0 MB |

Regenerate the full per-package breakdown from a built image with
`scripts/resolve_frappe_deps.py`, and verify a build against the exclusion list
with `scripts/check_pruned_deps.py`.

### Packages that look removable but are not

Five distributions carry **do not remove**: `Whoosh`, `openpyxl`, `xlsxwriter`,
`xlrd`, `et-xmlfile`. They look Desk-only and never appear in a boot trace, but
ERPNext controllers reach them transitively — removing them broke 40 modules
including `sales_order`, `stock_entry` and `work_order`. Decide exclusions from a
dependency graph, not from a boot trace: a package several kept packages depend
on is a very different proposition from a leaf.

> Inventory sizes are measured on **arm64**, so they are internally consistent
> but larger than what ships. The same build is 948 MB upstream / 433 MB slimmed
> on arm64, and **371 MB slimmed on linux/amd64**, which is what CI reports.

---

## What CI checks

| Job | When | What it covers |
|---|---|---|
| `test` | every push / PR | unit suite |
| `integration-test` | every push / PR | `tests/integration/` **inside the central-site bench container** — validates the library, but has the full unpruned Frappe install, so it cannot detect a bad exclusion |
| `build` → *Verify nothing broke* | every push / PR, **before the push** | `check_pruned_deps.py` against a locally built amd64 image. A broken exclusion list blocks publication |
| `verify-image` | pushes to **main**, or manual dispatch | `run_service_image_integration.sh` — `tests/integration/` from inside the *published* image against a real database |

The import-level gate sits in `build` because it is cheap: it builds amd64 only
and loads it locally, so the multi-arch push that follows reuses the buildx cache
and only arm64 is new work.

`verify-image` is deliberately **not** run per-PR. It pulls the central-site image
(several GB), which `integration-test` already pulls on its own runner, and the
gate in `build` already stops a broken image from being published. It pulls the
image `build` just pushed rather than rebuilding it.

### Architecture

The image is built for **linux/amd64 only**.

GitHub-hosted ARM runners (`ubuntu-24.04-arm`) are free for public repositories
only. This repo is private on a free plan, so an arm64 job would never be
assigned a runner. The alternative — emulating arm64 through QEMU — cost about
14.6 minutes per build for an architecture nothing currently deploys on, so it
was removed along with the QEMU setup step.

To add arm64 later, follow the pattern in `a sibling project`'s
`container-build.yml`: a native runner per architecture pushing `:tag-amd64` and
`:tag-arm64`, then `docker buildx imagetools create` to stitch the manifest, plus
a check that the published manifest really lists both. That needs a paid plan, a
public repo, or self-hosted ARM runners.

Note that image sizes differ by architecture: the same build measures 433 MB on
an arm64 host and **371 MB on linux/amd64**, which is what CI reports and what
gets deployed.
