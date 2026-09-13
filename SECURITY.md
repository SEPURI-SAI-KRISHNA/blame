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

## Reporting a vulnerability

Open a [security advisory](https://github.com/SEPURI-SAI-KRISHNA/blame/security/advisories/new)
rather than a public issue. I will acknowledge within a week.
