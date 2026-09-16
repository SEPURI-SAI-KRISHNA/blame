# Security

## Scope

`blame` runs entirely on your machine. It reads the DataFrames your code
creates, writes them to a local `.blame/` directory, and `blame ui` serves them
over HTTP bound to `127.0.0.1`. Nothing is sent anywhere.

Two things worth knowing:

- **`.blame/` contains your data.** Intermediate frames are stored so they can
  be shown later. Add it to `.gitignore`, and treat it with the same care as
  the input files. `sample_rows=N` caps how much of each frame is stored.
- **`blame ui` has no authentication.** It binds to localhost and is meant for
  a single developer on one machine. Do not expose it on a shared host.
- **A `.blame/` store from someone else is untrusted input.** `blame load`
  takes a run id, so "send me your `.blame/`" is a natural thing to ask of a
  debugging tool -- and opening one hands Arrow IPC files to a deserializer
  written in C++. The `pyarrow` floor is set past every version with a
  published advisory affecting that parser, but a floor is a statement about
  versions that exist today. Treat a store you did not record the way you would
  treat any other file from a stranger.

## Verifying a release

Every file on PyPI from 0.1.2 onwards carries
[PEP 740](https://peps.python.org/pep-0740/) provenance: a signed statement of
which repository and workflow built it. Releases are published through
GitHub's OIDC identity under trusted publishing, so there is no API token
anywhere that could be used to upload a file this does not cover.

PyPI shows it on the release's files page, and it can be read directly:

```bash
python -c "import json,urllib.request as u; print(json.load(u.urlopen('https://pypi.org/integrity/pandas-blame/0.1.3/pandas_blame-0.1.3-py3-none-any.whl/provenance'))['attestation_bundles'][0]['publisher'])"
```

```
{'environment': 'pypi', 'kind': 'GitHub', 'repository': 'SEPURI-SAI-KRISHNA/blame', 'workflow': 'release.yml'}
```

A file that does not answer with that repository and workflow was not published
from here.

One trap worth knowing if you go checking: `https://pypi.org/pypi/<name>/<version>/json`
reports `"provenance": null` for files that do have it. The simple index
(`Accept: application/vnd.pypi.simple.v1+json`) and the `/integrity/` endpoint
above are the ones that answer.

## Reporting a vulnerability

Open a [security advisory](https://github.com/SEPURI-SAI-KRISHNA/blame/security/advisories/new)
rather than a public issue. I will acknowledge within a week.
