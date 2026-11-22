#!/usr/bin/env bash
# Export a committed source snapshot into a new Git repository.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  printf 'Usage: %s DESTINATION [REVISION]\n' "$0" >&2
  exit 2
fi

source_root=$(git -C "$(dirname -- "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)
revision=$(git -C "$source_root" rev-parse --verify "${2:-HEAD}^{commit}")
parent=$(cd -- "$(dirname -- "$1")" && pwd -P)
destination="$parent/$(basename -- "$1")"
if [[ -e "$destination" || -L "$destination" ]]; then
  printf 'Destination already exists: %s\n' "$destination" >&2
  exit 2
fi

git_author=$(git -C "$source_root" show -s --format=%an "$revision")
git_email=$(git -C "$source_root" show -s --format=%ae "$revision")
stage=$(mktemp -d -- "$parent/.rag-export.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
git -C "$source_root" ls-tree -rz --full-tree --format='%(objectmode) %(path)' "$revision" >"$stage/source-files"
files=()
while IFS= read -r -d '' entry; do
  mode=${entry%% *}
  file=${entry#* }
  case "$file" in
    docs/context/RAG_ВИНИТИ_*|web/node_modules|web/node_modules/*|web/dist/*|qa/java/build/*|qa/java/.gradle/*|experiments/entity-passport/*/*) continue ;;
  esac
  case "/$file" in
    /.env.example) ;;
    */.env|*/.env.*|*/.npmrc|*/.netrc|*.[pP][eE][mM]|*.[pP]12|*.[pP][fF][xX]|*.[kK][eE][yY]|*/id_rsa|*/id_ed25519) continue ;;
  esac
  if [[ "${file,,}" == *.pdf && "$file" != qa/java/src/test/resources/synthetic.pdf ]]; then
    continue
  fi
  case "$file" in
    .github/*|qa/*|rag_mvp/*|web/*|scripts/*|tests/*|docs/*|    runs/check_saved_outputs.py|runs/d001_load_baseline.py|runs/full_pdf_batch.py|runs/snapshots/*|    experiments/entity-passport/*.py|.scratch/test-submission-build/publication-review.json|    compose.yaml|Dockerfile|requirements*.txt|rag|README.md|GLOSSARY.md|.gitignore|.dockerignore|.env.example)
      if [[ "$mode" != 100644 && "$mode" != 100755 ]]; then
        printf 'Unexpected Git entry in the export: %s\n' "$file" >&2
        exit 1
      fi
      files+=("$file")
      ;;
  esac
done <"$stage/source-files"
rm -- "$stage/source-files"

git -C "$source_root" archive --format=tar "$revision" -- "${files[@]}" | tar -xf - -C "$stage"
for entry in .github/workflows/qa.yml .github/scripts/qa_summary.py docs/data-passport.md \
  .scratch/test-submission-build/publication-review.json runs/full_pdf_batch.py \
  experiments/entity-passport/extract.py qa/java/gradle/wrapper/gradle-wrapper.jar web/pnpm-lock.yaml; do
  test -f "$stage/$entry"
done
for entry in qa/java/gradlew qa/run_workers.sh rag; do
  test -x "$stage/$entry"
done
git -C "$stage" init --quiet --initial-branch=main
git -C "$stage" -c core.filemode=true add -f -- .
git -C "$stage" -c user.name="$git_author" -c user.email="$git_email" -c core.hooksPath=/dev/null \
  commit --quiet --no-gpg-sign -m "Initial RAG source export" -m "Source revision: $revision"
mv -T --no-clobber -- "$stage" "$destination"
if [[ -d "$stage" ]]; then
  printf 'Destination appeared during export: %s\n' "$destination" >&2
  exit 2
fi
printf 'Exported %s to %s\n' "$revision" "$destination"
