#!/usr/bin/env bash
# Cut a ktool release: stamp the version in pyproject.toml and CHANGELOG.md,
# commit, and tag.
#
# Usage: scripts/release.sh <version> [--dry-run]
#
# <version> is a bare X.Y.Z with no leading "v"; the tag gets the v.
#
# The release workflow refuses to publish a tag that disagrees with
# pyproject.toml, so both must be in sync before pushing.
#
# This never pushes. It prints the push command and stops, because pushing
# a signed tag is the point of no return.

set -euo pipefail

VERSION=${1:-}
DRY_RUN=${2:-}

die() { echo "release: $*" >&2; exit 1; }

[ -n "${VERSION}" ] || die "usage: scripts/release.sh <version> [--dry-run]"
case "${DRY_RUN}" in
  ""|--dry-run) ;;
  *) die "unknown argument '${DRY_RUN}' (expected --dry-run)" ;;
esac

[[ ${VERSION} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version '${VERSION}' is not X.Y.Z"

ROOT=$(git rev-parse --show-toplevel) || die "not in a git checkout"
cd "${ROOT}"

PYPROJECT=ktool/pyproject.toml
CHANGELOG=CHANGELOG.md
TAG="v${VERSION}"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "${BRANCH}" = "main" ] || die "on branch '${BRANCH}', releases are cut from main"

[ -z "$(git status --porcelain)" ] || die "working tree is dirty, commit or stash first"

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  die "tag ${TAG} already exists"
fi

if git ls-remote --exit-code --tags origin "${TAG}" >/dev/null 2>&1; then
  die "tag ${TAG} already exists on origin"
fi

CURRENT=$(sed -n 's/^version = "\(.*\)"$/\1/p' "${PYPROJECT}" | head -1)
[ -n "${CURRENT}" ] || die "no version found in ${PYPROJECT}"
[ "${CURRENT}" != "${VERSION}" ] || die "${PYPROJECT} is already at ${VERSION}"
NEWEST=$(printf '%s\n%s\n' "${CURRENT}" "${VERSION}" | sort -V | tail -1)
[ "${NEWEST}" = "${VERSION}" ] || die "${VERSION} is older than the current ${CURRENT}"

grep -q '^## \[Unreleased\]$' "${CHANGELOG}" || die "no '## [Unreleased]' heading in ${CHANGELOG}"

unreleased_body() {
  awk '/^## \[Unreleased\]$/ { inside = 1; next }
       /^## \[/              { inside = 0 }
       inside' "${CHANGELOG}"
}

[ -n "$(unreleased_body | grep '^- ' || true)" ] ||
  die "${CHANGELOG} has no entries under [Unreleased], nothing to release"

TODAY=$(date -u +%Y-%m-%d)

if [ "${DRY_RUN}" = "--dry-run" ]; then
  echo "release: would stamp ${CURRENT} -> ${VERSION} and tag ${TAG}"
  echo "release: would retitle [Unreleased] as [${TAG}] - ${TODAY} in ${CHANGELOG}"
  exit 0
fi

COMMITTED=no
restore() {
  if [ "${COMMITTED}" = yes ]; then return; fi
  git checkout HEAD -- "${PYPROJECT}" "${CHANGELOG}" 2>/dev/null || true
  echo "release: failed, working tree restored" >&2
}
trap restore EXIT

sed -i "0,/^version = \"${CURRENT}\"$/s//version = \"${VERSION}\"/" "${PYPROJECT}"
[ "$(sed -n 's/^version = "\(.*\)"$/\1/p' "${PYPROJECT}" | head -1)" = "${VERSION}" ] ||
  die "failed to stamp ${PYPROJECT}"

# Leave an empty [Unreleased] for the next cycle.
awk -v tag="${TAG}" -v today="${TODAY}" '
  /^## \[Unreleased\]$/ { print; print ""; printf "## [%s] - %s\n", tag, today; next }
  { print }
' "${CHANGELOG}" > "${CHANGELOG}.new"
mv "${CHANGELOG}.new" "${CHANGELOG}"

git add "${PYPROJECT}" "${CHANGELOG}"
git commit -s -m "chore(release): ${TAG}" \
  -m "Stamp ${VERSION} in ${PYPROJECT} and ${CHANGELOG}."
COMMITTED=yes

git tag -s "${TAG}" -m "ktool ${TAG}"

cat <<EOF

release: ${TAG} committed and signed locally. Nothing has been pushed.

Review it:

  git show ${TAG}

Then publish (triggers the release workflow):

  git push origin main ${TAG}
EOF
