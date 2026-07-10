# powermetrics fixtures

Each `*.plist` file here is one powermetrics sample. The test suite parses
every file in this directory and asserts structural invariants, so dropping
in a new fixture from a different chip or macOS version extends coverage
with zero test code.

`m2pro-power-keys` / `energy-keys` are synthetic, modeled on real output —
one for macOS versions that report power in mW, one for versions that report
energy in mJ. `mac15-9-m3max-macos27.plist` is a real capture (M3 Max,
macOS 27) that pins that version's quirks. Real captures are better; to
record one from your machine:

```sh
sudo powermetrics --samplers cpu_power,gpu_power,ane_power -i 1000 -n 1 -f plist \
  | tr -d '\000' > tests/fixtures/<chip>-<macos-version>.plist
```

e.g. `m4max-sequoia-15.5.plist`. The `tr` strips the NUL sample separator so
the file is a single well-formed plist.
