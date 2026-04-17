# homebrew-monmon (tap scaffold)

This directory is **not** a live Homebrew tap. It's a staging area. To ship
monmon via Homebrew you need a separate GitHub repository named exactly
`homebrew-monmon` under your user (e.g. `github.com/gavi/homebrew-monmon`),
with this `Formula/` directory at its root.

## Publish workflow

Run these from the main `monmon` repo once:

```sh
# 1. Build + publish to PyPI (you need a PyPI account + API token).
uv build
uv publish

# 2. Tag the release and push to GitHub.
git tag v0.1.0
git push origin main --tags

# 3. Create the tap repo on GitHub: gavi/homebrew-monmon (empty repo).
#    Then bootstrap it from this staging directory:
cd /tmp
git clone https://github.com/gavi/homebrew-monmon.git
cp -R /Users/gavi/work/ai/monmon/packaging/homebrew-tap/Formula homebrew-monmon/
cd homebrew-monmon

# 4. Fill in the sdist sha256 from PyPI, then let brew write the resource
#    stanzas for every transitive dependency.
brew update-python-resources Formula/monmon.rb

# 5. Lint + test locally.
brew install --build-from-source ./Formula/monmon.rb
brew audit --strict --online monmon

# 6. Commit + push the tap.
git add Formula/monmon.rb
git commit -m "monmon 0.1.0"
git push origin main
```

## Users install with

```sh
brew install gavi/monmon/monmon
```

(Short form for `brew tap gavi/monmon && brew install monmon`.)

## Updating the formula for a new release

```sh
# bump version in pyproject.toml, then:
uv build && uv publish
# in the tap repo:
brew bump-formula-pr --version=0.2.0 Formula/monmon.rb
# or edit url/sha256 by hand and re-run `brew update-python-resources`.
```
