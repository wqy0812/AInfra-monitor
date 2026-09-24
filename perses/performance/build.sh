#!/bin/bash
set -euo pipefail
PATCH_ROOT=$(cd "$(dirname "$0")" && pwd)
BUILD_ROOT=${1:?usage: build.sh NEW_BUILD_DIRECTORY [VERSION] [PREVIOUS_RELEASE_LOCK]}
RELEASE_VERSION=${2:-0.54.0-perf.3}
PREVIOUS_RELEASE_LOCK=${3:-$PATCH_ROOT/release-lock.json}
PREVIOUS_RELEASE_LOCK=$(cd "$(dirname "$PREVIOUS_RELEASE_LOCK")" && pwd)/$(basename "$PREVIOUS_RELEASE_LOCK")
test -f "$PREVIOUS_RELEASE_LOCK"
[[ "$RELEASE_VERSION" =~ ^0\.54\.0-perf\.[1-9][0-9]*$ ]]
test ! -e "$BUILD_ROOT/perses-0.54.0"
mkdir -p "$BUILD_ROOT"
BUILD_ROOT=$(cd "$BUILD_ROOT" && pwd)
python3 - "$PATCH_ROOT" "$BUILD_ROOT" <<'PY'
import sys,json,hashlib,urllib.request,tarfile,pathlib
patch,build=map(pathlib.Path,sys.argv[1:]);lock=json.loads((patch/'source-lock.json').read_text())
p=build/'source.tar.gz';urllib.request.urlretrieve('https://codeload.github.com/perses/perses/tar.gz/refs/tags/v0.54.0',p)
assert hashlib.sha256(p.read_bytes()).hexdigest()==lock['source_archive_sha256']
with tarfile.open(p) as t:t.extractall(build,filter='data')
s=build/'perses-0.54.0';assert hashlib.sha256((s/'ui/package-lock.json').read_bytes()).hexdigest()==lock['npm_lock_sha256']
PY
PERSES_SOURCE="$BUILD_ROOT/perses-0.54.0"
cd "$PERSES_SOURCE/ui"
npm ci --ignore-scripts --no-audit --no-fund
node "$PATCH_ROOT/apply-ui.cjs" "$PERSES_SOURCE"
npm run build --workspace=@perses-dev/core --workspace=@perses-dev/app
cd "$PERSES_SOURCE"
patch -p1 < "$PATCH_ROOT/endpoint.patch"
cp "$PATCH_ROOT/cache.go" ui/performance_cache.go
cp "$PATCH_ROOT/tests/cache_test.go" ui/performance_cache_test.go
bash scripts/compress_assets.sh
PERSES_SOURCE="$PERSES_SOURCE" ui/node_modules/.bin/jest --config "$PATCH_ROOT/tests/jest.config.cjs" --runInBand
# Prepare dependencies separately; compilation must use the installed toolchain
# and the module-local vendor tree without downloading anything.
export GOTOOLCHAIN=local
go mod tidy
go mod vendor
export GOPROXY=off GOSUMDB=off
go test -mod=vendor ./ui
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -mod=vendor -ldflags "-s -w -X github.com/prometheus/common/version.Version=$RELEASE_VERSION -X github.com/prometheus/common/version.Revision=4c719fc19fa21d333797e84c4fe7e3d81c25f4f5+performance" -o "$BUILD_ROOT/perses" ./cmd/perses
cp "$PATCH_ROOT/Dockerfile" "$BUILD_ROOT/Dockerfile"
docker build --platform linux/amd64 --build-arg PERF_VERSION="$RELEASE_VERSION" -t "monitoring-perses:$RELEASE_VERSION" "$BUILD_ROOT"
docker save --platform linux/amd64 "monitoring-perses:$RELEASE_VERSION" | gzip -n > "$BUILD_ROOT/perses-$RELEASE_VERSION.tar.gz"
shasum -a 256 "$BUILD_ROOT/perses" "$BUILD_ROOT/perses-$RELEASE_VERSION.tar.gz" > "$BUILD_ROOT/sha256sums.txt"

python3 - "$PATCH_ROOT" "$BUILD_ROOT" "$RELEASE_VERSION" "$PREVIOUS_RELEASE_LOCK" <<'PYLOCK'
import sys,json,pathlib,hashlib,tarfile
patch,build=map(pathlib.Path,sys.argv[1:3]);version=sys.argv[3];lock=json.loads((patch/'source-lock.json').read_text());archive=build/('perses-'+version+'.tar.gz')
previous=json.loads(pathlib.Path(sys.argv[4]).read_text())
assert previous['candidate_version']!=version,'Use a new release version'
lock.update(previous_version=previous['candidate_version'],previous_image_digest=previous['candidate_config_digest'])
with tarfile.open(archive) as t:
 manifest=json.load(t.extractfile('manifest.json'))[0];config=t.extractfile(manifest['Config']).read()
lock.update(candidate_version=version,candidate_archive_name=archive.name,candidate_archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),candidate_config_digest='sha256:'+hashlib.sha256(config).hexdigest())
(build/'release-lock.json').write_text(json.dumps(lock,indent=2)+'\n')
PYLOCK
